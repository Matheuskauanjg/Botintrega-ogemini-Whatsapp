import 'dotenv/config';

import fs from 'node:fs/promises';
import path from 'node:path';
import express from 'express';
import QRCode from 'qrcode';
import pino from 'pino';
import sharp from 'sharp';
import makeWASocket, {
  DisconnectReason,
  downloadMediaMessage,
  getContentType,
  useMultiFileAuthState
} from '@whiskeysockets/baileys';

const app = express();
app.use(express.json({ limit: '20mb' }));

const PORT = Number(process.env.PORT || 10000);
const API_TOKEN = process.env.API_TOKEN || '';
const QR_SECRET = process.env.QR_SECRET || '';
const AUTH_PATH = process.env.BAILEYS_AUTH_PATH || path.resolve(process.cwd(), '.baileys_auth');
const logger = pino({ level: process.env.BAILEYS_LOG_LEVEL || 'silent' });

const MAX_MESSAGES_PER_CHAT = 300;
const MAX_IMAGE_INPUT_BYTES = 12 * 1024 * 1024;
const MAX_AUDIO_BYTES = 12 * 1024 * 1024;

let sock = null;
let whatsappState = 'starting';
let lastError = null;
let latestQrDataUrl = null;
let latestQrAt = null;
let me = null;
let reconnectTimer = null;
let socketGeneration = 0;

const chats = new Map();
const contacts = new Map();
const messagesByChat = new Map();

function requireApiToken(req, res, next) {
  if (!API_TOKEN) return res.status(503).json({ error: 'API_TOKEN is not configured' });
  const auth = String(req.headers.authorization || '');
  const bearer = auth.startsWith('Bearer ') ? auth.slice(7) : '';
  const apiKey = String(req.headers['x-api-key'] || '');
  if (bearer !== API_TOKEN && apiKey !== API_TOKEN) return res.status(401).json({ error: 'Unauthorized' });
  next();
}

function requireQrSecret(req, res, next) {
  if (!QR_SECRET) return next();
  if (req.query.key === QR_SECRET || req.headers['x-qr-secret'] === QR_SECRET) return next();
  return res.status(401).json({ error: 'Invalid QR secret' });
}

function toNumber(value) {
  if (value == null) return null;
  if (typeof value === 'number') return value;
  if (typeof value === 'bigint') return Number(value);
  if (typeof value?.toNumber === 'function') return value.toNumber();
  const parsed = Number(value?.toString?.() ?? value);
  return Number.isFinite(parsed) ? parsed : null;
}

function normalizeMessageContent(message) {
  let content = message?.message || null;
  while (content) {
    if (content.ephemeralMessage?.message) { content = content.ephemeralMessage.message; continue; }
    if (content.viewOnceMessage?.message) { content = content.viewOnceMessage.message; continue; }
    if (content.viewOnceMessageV2?.message) { content = content.viewOnceMessageV2.message; continue; }
    if (content.documentWithCaptionMessage?.message) { content = content.documentWithCaptionMessage.message; continue; }
    if (content.associatedChildMessage?.message) { content = content.associatedChildMessage.message; continue; }
    break;
  }
  return content;
}

function extractText(message) {
  const content = normalizeMessageContent(message);
  if (!content) return '';
  return content.conversation
    || content.extendedTextMessage?.text
    || content.imageMessage?.caption
    || content.videoMessage?.caption
    || content.documentMessage?.caption
    || content.buttonsResponseMessage?.selectedDisplayText
    || content.listResponseMessage?.title
    || content.templateButtonReplyMessage?.selectedDisplayText
    || '';
}

function audioInfo(content) {
  const audio = content?.audioMessage;
  if (audio) {
    return {
      available: true,
      mimetype: audio.mimetype || 'audio/ogg; codecs=opus',
      seconds: toNumber(audio.seconds),
      ptt: Boolean(audio.ptt)
    };
  }
  const doc = content?.documentMessage;
  if (doc?.mimetype?.startsWith?.('audio/')) {
    return {
      available: true,
      mimetype: doc.mimetype,
      seconds: null,
      ptt: false
    };
  }
  return null;
}

function serializeMessage(message) {
  const content = normalizeMessageContent(message);
  return {
    id: message?.key?.id || null,
    chatId: message?.key?.remoteJid || null,
    participant: message?.key?.participant || null,
    fromMe: Boolean(message?.key?.fromMe),
    text: extractText(message),
    timestamp: toNumber(message?.messageTimestamp),
    type: content ? getContentType(content) || null : null,
    pushName: message?.pushName || null,
    audio: audioInfo(content)
  };
}

function contactName(jid) {
  const contact = contacts.get(jid);
  return contact?.name || contact?.notify || contact?.verifiedName || null;
}

function serializeChat(chat) {
  const id = chat?.id || chat?.jid || null;
  return {
    id,
    name: chat?.name || contactName(id) || null,
    unreadCount: Number(chat?.unreadCount || 0),
    timestamp: toNumber(chat?.conversationTimestamp),
    archived: Boolean(chat?.archived),
    pinned: Boolean(chat?.pinned)
  };
}

function upsertChat(chat) {
  const id = chat?.id || chat?.jid;
  if (!id) return;
  chats.set(id, { ...(chats.get(id) || {}), ...chat, id });
}

function upsertContact(contact) {
  const id = contact?.id;
  if (!id) return;
  contacts.set(id, { ...(contacts.get(id) || {}), ...contact });
}

function cacheMessage(message) {
  const jid = message?.key?.remoteJid;
  if (!jid) return;
  const current = messagesByChat.get(jid) || [];
  const id = message?.key?.id;
  const withoutDuplicate = id ? current.filter(item => item?.key?.id !== id) : current;
  withoutDuplicate.push(message);
  withoutDuplicate.sort((a, b) => (toNumber(a?.messageTimestamp) || 0) - (toNumber(b?.messageTimestamp) || 0));
  if (withoutDuplicate.length > MAX_MESSAGES_PER_CHAT) {
    withoutDuplicate.splice(0, withoutDuplicate.length - MAX_MESSAGES_PER_CHAT);
  }
  messagesByChat.set(jid, withoutDuplicate);
  upsertChat({ id: jid, conversationTimestamp: toNumber(message?.messageTimestamp) || Math.floor(Date.now() / 1000) });
}

function findMessage(chatId, messageId) {
  const items = messagesByChat.get(chatId) || [];
  return items.find(item => item?.key?.id === messageId) || null;
}

function jidFromDestination(value) {
  const raw = String(value || '').trim();
  if (!raw) return null;
  if (raw.includes('@')) return raw;
  const digits = raw.replace(/\D/g, '');
  if (digits.length < 10) return null;
  return `${digits}@s.whatsapp.net`;
}

function isPrivateHostname(hostname) {
  const host = String(hostname || '').toLowerCase().replace(/^\[|\]$/g, '');
  if (!host) return true;
  if (host === 'localhost' || host === '::1' || host.endsWith('.local')) return true;
  if (/^127\./.test(host) || /^10\./.test(host) || /^169\.254\./.test(host) || /^192\.168\./.test(host)) return true;
  const match172 = host.match(/^172\.(\d+)\./);
  return Boolean(match172 && Number(match172[1]) >= 16 && Number(match172[1]) <= 31);
}

async function readRemoteBuffer(urlText, maxBytes, label) {
  let url;
  try { url = new URL(urlText); } catch { throw new Error(`Invalid ${label} URL`); }
  if (!['http:', 'https:'].includes(url.protocol)) throw new Error(`${label} URL must use http or https`);
  if (isPrivateHostname(url.hostname)) throw new Error(`Private or local ${label} URLs are not allowed`);

  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 20000);
  try {
    const response = await fetch(url, { redirect: 'follow', signal: controller.signal });
    if (!response.ok) throw new Error(`Could not download ${label}: HTTP ${response.status}`);
    const declared = Number(response.headers.get('content-length') || 0);
    if (declared > maxBytes) throw new Error(`${label} exceeds size limit`);
    const buffer = Buffer.from(await response.arrayBuffer());
    if (!buffer.length) throw new Error(`Downloaded ${label} is empty`);
    if (buffer.length > maxBytes) throw new Error(`${label} exceeds size limit`);
    return { buffer, contentType: String(response.headers.get('content-type') || '').split(';')[0].trim().toLowerCase() };
  } finally {
    clearTimeout(timeout);
  }
}

async function resolveImageInput(body) {
  const imageUrl = String(body?.imageUrl || '').trim();
  const imageBase64 = String(body?.imageBase64 || '').trim();
  let buffer;
  let source;

  if (imageBase64) {
    let encoded = imageBase64;
    const dataUri = imageBase64.match(/^data:image\/[a-zA-Z0-9.+-]+;base64,(.+)$/s);
    if (dataUri) encoded = dataUri[1];
    buffer = Buffer.from(encoded, 'base64');
    source = 'base64';
  } else if (imageUrl) {
    const remote = await readRemoteBuffer(imageUrl, MAX_IMAGE_INPUT_BYTES, 'image');
    if (remote.contentType && !remote.contentType.startsWith('image/')) throw new Error(`URL is not an image (${remote.contentType})`);
    buffer = remote.buffer;
    source = 'url';
  } else {
    throw new Error('imageUrl or imageBase64 is required');
  }

  if (!buffer?.length) throw new Error('Image is empty or invalid');
  if (buffer.length > MAX_IMAGE_INPUT_BYTES) throw new Error('Image exceeds 12 MB input limit');

  // Normalize everything to a conservative WhatsApp-compatible JPEG. This avoids
  // corrupt previews caused by mismatched MIME types, WebP/AVIF variants, EXIF
  // orientation, or image URLs whose Content-Type does not match the bytes.
  let normalized = await sharp(buffer, { failOn: 'none', animated: false })
    .rotate()
    .resize({ width: 4096, height: 4096, fit: 'inside', withoutEnlargement: true })
    .flatten({ background: '#ffffff' })
    .jpeg({ quality: 88, mozjpeg: true })
    .toBuffer();

  if (normalized.length > 8 * 1024 * 1024) {
    normalized = await sharp(buffer, { failOn: 'none', animated: false })
      .rotate()
      .resize({ width: 3072, height: 3072, fit: 'inside', withoutEnlargement: true })
      .flatten({ background: '#ffffff' })
      .jpeg({ quality: 76, mozjpeg: true })
      .toBuffer();
  }

  return { buffer: normalized, mimetype: 'image/jpeg', source };
}

function isReady() {
  return whatsappState === 'ready' && Boolean(sock);
}

function disconnectStatusCode(error) {
  return error?.output?.statusCode ?? error?.data?.statusCode ?? error?.statusCode ?? null;
}

function scheduleReconnect(delay = 1500) {
  clearTimeout(reconnectTimer);
  reconnectTimer = setTimeout(() => {
    connectWhatsApp().catch(error => {
      whatsappState = 'connection_error';
      lastError = error.message;
      console.error('[Baileys] Falha ao reconectar:', error);
      scheduleReconnect(Math.min(delay * 2, 15000));
    });
  }, delay);
}

async function clearInvalidSession() {
  try {
    await fs.rm(AUTH_PATH, { recursive: true, force: true });
    await fs.mkdir(AUTH_PATH, { recursive: true });
    console.log('[Baileys] Sessão inválida removida; novo QR será solicitado.');
  } catch (error) {
    console.error('[Baileys] Falha ao limpar sessão:', error);
  }
}

async function connectWhatsApp() {
  const generation = ++socketGeneration;
  whatsappState = 'connecting';
  lastError = null;
  await fs.mkdir(AUTH_PATH, { recursive: true });
  const { state, saveCreds } = await useMultiFileAuthState(AUTH_PATH);

  const currentSock = makeWASocket({
    auth: state,
    logger,
    markOnlineOnConnect: false,
    syncFullHistory: true,
    generateHighQualityLinkPreview: false
  });

  sock = currentSock;
  console.log(`[Baileys] Socket iniciado. Auth: ${AUTH_PATH}`);
  currentSock.ev.on('creds.update', saveCreds);

  currentSock.ev.on('messaging-history.set', ({ chats: historyChats, contacts: historyContacts, messages }) => {
    for (const chat of historyChats || []) upsertChat(chat);
    for (const contact of historyContacts || []) upsertContact(contact);
    for (const message of messages || []) cacheMessage(message);
    console.log(`[Baileys] Histórico: ${historyChats?.length || 0} chats, ${messages?.length || 0} mensagens.`);
  });
  currentSock.ev.on('chats.upsert', update => { for (const chat of update || []) upsertChat(chat); });
  currentSock.ev.on('chats.update', update => { for (const chat of update || []) upsertChat(chat); });
  currentSock.ev.on('contacts.upsert', update => { for (const contact of update || []) upsertContact(contact); });
  currentSock.ev.on('contacts.update', update => { for (const contact of update || []) upsertContact(contact); });
  currentSock.ev.on('messages.upsert', ({ messages }) => { for (const message of messages || []) cacheMessage(message); });

  currentSock.ev.on('connection.update', async update => {
    if (generation !== socketGeneration) return;
    const { connection, lastDisconnect, qr } = update;

    if (qr) {
      try {
        latestQrDataUrl = await QRCode.toDataURL(qr, { width: 560, margin: 3, errorCorrectionLevel: 'M' });
        latestQrAt = new Date().toISOString();
        whatsappState = 'waiting_for_qr_scan';
        lastError = null;
        console.log('[Baileys] QR gráfico pronto em /qr');
      } catch (error) {
        whatsappState = 'qr_error';
        lastError = error.message;
      }
    }

    if (connection === 'connecting' && !qr && whatsappState !== 'waiting_for_qr_scan') whatsappState = 'connecting';

    if (connection === 'open') {
      latestQrDataUrl = null;
      latestQrAt = null;
      whatsappState = 'ready';
      lastError = null;
      me = currentSock.user ? { id: currentSock.user.id || null, name: currentSock.user.name || null } : null;
      console.log('[Baileys] WhatsApp conectado.');
      return;
    }

    if (connection === 'close') {
      const error = lastDisconnect?.error;
      const statusCode = disconnectStatusCode(error);
      const loggedOut = statusCode === DisconnectReason.loggedOut;
      latestQrDataUrl = null;
      latestQrAt = null;
      me = null;
      console.warn(`[Baileys] Conexão fechada. status=${statusCode ?? 'unknown'} loggedOut=${loggedOut}`);

      if (loggedOut) {
        whatsappState = 'logged_out';
        lastError = 'Sessão desconectada do WhatsApp. Gerando uma nova sessão.';
        await clearInvalidSession();
        scheduleReconnect(1000);
        return;
      }
      whatsappState = statusCode === DisconnectReason.restartRequired ? 'restarting' : 'reconnecting';
      lastError = error?.message || null;
      scheduleReconnect(statusCode === DisconnectReason.restartRequired ? 500 : 1500);
    }
  });
}

app.get('/', (_req, res) => {
  res.type('html').send(`<!doctype html><html lang="pt-BR"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>WhatsApp Personal Bridge</title></head><body style="font-family:Arial,sans-serif;max-width:760px;margin:40px auto;padding:0 20px"><h1>WhatsApp Personal Bridge · Media v2</h1><p>Status: <strong>${whatsappState}</strong></p><p><a href="/qr">Abrir QR Code</a></p></body></html>`);
});

app.get('/health', (_req, res) => {
  res.json({ ok: true, service: 'whatsapp-personal-render-media-v2', whatsappState, ready: isReady(), hasQr: Boolean(latestQrDataUrl), qrGeneratedAt: latestQrAt, cachedChats: chats.size, authPath: AUTH_PATH, lastError });
});

app.get('/qr', (req, res) => {
  if (QR_SECRET && req.query.key !== QR_SECRET) return res.status(401).type('html').send('<h1>401 - chave do QR inválida</h1>');
  let content;
  if (latestQrDataUrl) content = `<img src="${latestQrDataUrl}" alt="QR Code" style="width:min(560px,100%)"><p>WhatsApp → Dispositivos conectados → Conectar dispositivo.</p>`;
  else if (whatsappState === 'ready') content = '<h2>✓ WhatsApp conectado</h2>';
  else content = `<h2>Gerando QR...</h2><p>${whatsappState}</p>`;
  res.type('html').send(`<!doctype html><html lang="pt-BR"><head><meta charset="utf-8"><meta http-equiv="refresh" content="4"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Conectar WhatsApp</title></head><body style="font-family:Arial,sans-serif;max-width:680px;margin:40px auto;text-align:center">${content}</body></html>`);
});

app.post('/qr/reset', requireQrSecret, async (_req, res) => {
  try {
    socketGeneration += 1;
    try { sock?.end?.(new Error('manual session reset')); } catch (_) {}
    sock = null;
    latestQrDataUrl = null;
    latestQrAt = null;
    me = null;
    whatsappState = 'resetting';
    await clearInvalidSession();
    scheduleReconnect(300);
    res.json({ ok: true });
  } catch (error) {
    res.status(500).json({ error: error.message });
  }
});

app.get('/api/status', requireApiToken, (_req, res) => {
  res.json({ state: whatsappState, ready: isReady(), me, cachedChats: chats.size, authPath: AUTH_PATH, lastError });
});

app.get('/api/chats', requireApiToken, (req, res) => {
  const limit = Math.min(Math.max(Number(req.query.limit || 30), 1), 100);
  const result = Array.from(chats.values()).map(serializeChat).sort((a, b) => (b.timestamp || 0) - (a.timestamp || 0)).slice(0, limit);
  res.json({ chats: result });
});

app.get('/api/chats/:chatId/messages', requireApiToken, (req, res) => {
  const limit = Math.min(Math.max(Number(req.query.limit || 30), 1), 100);
  const items = (messagesByChat.get(req.params.chatId) || []).slice(-limit).map(serializeMessage);
  res.json({ chatId: req.params.chatId, messages: items });
});

app.get('/api/search', requireApiToken, (req, res) => {
  const q = String(req.query.q || '').trim().toLowerCase();
  if (!q) return res.status(400).json({ error: 'Query parameter q is required' });
  const results = [];
  for (const [chatId, items] of messagesByChat.entries()) {
    for (const message of items) {
      const serialized = serializeMessage(message);
      if ((serialized.text || '').toLowerCase().includes(q)) results.push({ chatId, message: serialized });
    }
  }
  results.sort((a, b) => (b.message.timestamp || 0) - (a.message.timestamp || 0));
  res.json({ query: q, results: results.slice(0, 100) });
});

app.post('/api/send', requireApiToken, async (req, res) => {
  try {
    if (!isReady()) return res.status(409).json({ error: 'WhatsApp is not ready', state: whatsappState });
    const jid = jidFromDestination(req.body?.to);
    const message = String(req.body?.message || '').trim();
    if (!jid) return res.status(400).json({ error: 'Invalid phone number or JID' });
    if (!message) return res.status(400).json({ error: 'Message is required' });
    if (message.length > 5000) return res.status(400).json({ error: 'Message is too long' });
    const sent = await sock.sendMessage(jid, { text: message });
    if (sent) cacheMessage(sent);
    res.json({ ok: true, id: sent?.key?.id || null, to: jid, timestamp: toNumber(sent?.messageTimestamp) });
  } catch (error) {
    res.status(500).json({ error: error.message });
  }
});

app.post('/api/send-image', requireApiToken, async (req, res) => {
  try {
    if (!isReady()) return res.status(409).json({ error: 'WhatsApp is not ready', state: whatsappState });
    const jid = jidFromDestination(req.body?.to);
    const caption = String(req.body?.caption || '').trim();
    if (!jid) return res.status(400).json({ error: 'Invalid phone number or JID' });
    if (caption.length > 5000) return res.status(400).json({ error: 'Caption is too long' });
    const image = await resolveImageInput(req.body || {});
    const sent = await sock.sendMessage(jid, { image: image.buffer, mimetype: 'image/jpeg', ...(caption ? { caption } : {}) });
    if (sent) cacheMessage(sent);
    res.json({ ok: true, id: sent?.key?.id || null, to: jid, timestamp: toNumber(sent?.messageTimestamp), bytes: image.buffer.length, mimetype: 'image/jpeg', source: image.source, normalized: true });
  } catch (error) {
    const message = error?.name === 'AbortError' ? 'Timed out downloading image' : error.message;
    console.error('[Media] send-image error:', message);
    res.status(400).json({ error: message });
  }
});

app.post('/api/audio', requireApiToken, async (req, res) => {
  try {
    if (!isReady()) return res.status(409).json({ error: 'WhatsApp is not ready', state: whatsappState });
    const chatId = String(req.body?.chatId || '').trim();
    const messageId = String(req.body?.messageId || '').trim();
    if (!chatId || !messageId) return res.status(400).json({ error: 'chatId and messageId are required' });

    const message = findMessage(chatId, messageId);
    if (!message) return res.status(404).json({ error: 'Audio message is not present in the in-memory cache' });
    const content = normalizeMessageContent(message);
    const info = audioInfo(content);
    if (!info) return res.status(400).json({ error: 'Selected message is not an audio message' });

    const downloaded = await downloadMediaMessage(
      message,
      'buffer',
      {},
      { logger, reuploadRequest: sock.updateMediaMessage }
    );
    const buffer = Buffer.isBuffer(downloaded) ? downloaded : Buffer.from(downloaded || []);
    if (!buffer.length) return res.status(410).json({ error: 'Audio media is no longer available' });
    if (buffer.length > MAX_AUDIO_BYTES) return res.status(413).json({ error: 'Audio exceeds 12 MB limit' });

    res.json({
      ok: true,
      chatId,
      messageId,
      mimetype: info.mimetype || 'audio/ogg; codecs=opus',
      seconds: info.seconds,
      ptt: info.ptt,
      bytes: buffer.length,
      audioBase64: buffer.toString('base64')
    });
  } catch (error) {
    const status = error?.output?.statusCode || error?.statusCode || 400;
    console.error('[Media] audio download error:', error?.message || error);
    res.status(Number.isInteger(status) && status >= 400 && status < 600 ? status : 400).json({ error: error?.message || 'Could not download audio' });
  }
});

const server = app.listen(PORT, '0.0.0.0', () => {
  console.log(`[HTTP] Media v2 listening on 0.0.0.0:${PORT}`);
  console.log(`[Baileys] Auth path: ${AUTH_PATH}`);
  connectWhatsApp().catch(error => {
    whatsappState = 'initialization_error';
    lastError = error.message;
    console.error('[Baileys] Initialization error:', error);
    scheduleReconnect(3000);
  });
});

async function shutdown(signal) {
  console.log(`[System] ${signal} recebido, encerrando.`);
  clearTimeout(reconnectTimer);
  socketGeneration += 1;
  try { sock?.end?.(new Error('server shutdown')); } catch (_) {}
  server.close(() => process.exit(0));
  setTimeout(() => process.exit(0), 3000).unref();
}

process.on('SIGTERM', () => shutdown('SIGTERM'));
process.on('SIGINT', () => shutdown('SIGINT'));
