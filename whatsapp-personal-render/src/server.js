import 'dotenv/config';

import fs from 'node:fs/promises';
import path from 'node:path';
import express from 'express';
import QRCode from 'qrcode';
import pino from 'pino';
import makeWASocket, {
  DisconnectReason,
  getContentType,
  useMultiFileAuthState
} from '@whiskeysockets/baileys';

const app = express();
app.use(express.json({ limit: '12mb' }));

const PORT = Number(process.env.PORT || 10000);
const API_TOKEN = process.env.API_TOKEN || '';
const QR_SECRET = process.env.QR_SECRET || '';
const PUBLIC_BASE_URL = process.env.PUBLIC_BASE_URL || '';

const legacyAuthPath = process.env.WWEBJS_AUTH_PATH
  ? process.env.WWEBJS_AUTH_PATH.replace(/\.wwebjs_auth\/?$/, 'baileys_auth')
  : null;

const AUTH_PATH = process.env.BAILEYS_AUTH_PATH
  || legacyAuthPath
  || path.resolve(process.cwd(), '.baileys_auth');

const logger = pino({ level: process.env.BAILEYS_LOG_LEVEL || 'silent' });

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
const MAX_MESSAGES_PER_CHAT = 250;
const MAX_IMAGE_BYTES = 8 * 1024 * 1024;

function requireApiToken(req, res, next) {
  if (!API_TOKEN) return res.status(503).json({ error: 'API_TOKEN is not configured' });

  const auth = req.headers.authorization || '';
  const bearer = auth.startsWith('Bearer ') ? auth.slice(7) : null;
  const apiKey = req.headers['x-api-key'];

  if (bearer !== API_TOKEN && apiKey !== API_TOKEN) {
    return res.status(401).json({ error: 'Unauthorized' });
  }

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
    if (content.ephemeralMessage?.message) {
      content = content.ephemeralMessage.message;
      continue;
    }
    if (content.viewOnceMessage?.message) {
      content = content.viewOnceMessage.message;
      continue;
    }
    if (content.viewOnceMessageV2?.message) {
      content = content.viewOnceMessageV2.message;
      continue;
    }
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
    pushName: message?.pushName || null
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

  upsertChat({
    id: jid,
    conversationTimestamp: toNumber(message?.messageTimestamp) || Math.floor(Date.now() / 1000)
  });
}

function jidFromDestination(value) {
  const raw = String(value || '').trim();
  if (!raw) return null;
  if (raw.includes('@')) return raw;

  const digits = raw.replace(/\D/g, '');
  if (digits.length < 10) return null;
  return `${digits}@s.whatsapp.net`;
}

function isPrivateImageHostname(hostname) {
  const host = String(hostname || '').toLowerCase().replace(/^\[|\]$/g, '');
  if (!host) return true;
  if (host === 'localhost' || host === '::1' || host.endsWith('.local')) return true;
  if (/^127\./.test(host) || /^10\./.test(host) || /^169\.254\./.test(host) || /^192\.168\./.test(host)) return true;
  const match172 = host.match(/^172\.(\d+)\./);
  if (match172 && Number(match172[1]) >= 16 && Number(match172[1]) <= 31) return true;
  return false;
}

async function resolveImageInput(body) {
  const imageUrl = String(body?.imageUrl || '').trim();
  const imageBase64 = String(body?.imageBase64 || '').trim();
  const requestedMime = String(body?.mimetype || '').trim();

  if (imageBase64) {
    let encoded = imageBase64;
    let mimetype = requestedMime || 'image/jpeg';
    const dataUri = imageBase64.match(/^data:(image\/[a-zA-Z0-9.+-]+);base64,(.+)$/s);
    if (dataUri) {
      mimetype = dataUri[1];
      encoded = dataUri[2];
    }

    const buffer = Buffer.from(encoded, 'base64');
    if (!buffer.length) throw new Error('imageBase64 is empty or invalid');
    if (buffer.length > MAX_IMAGE_BYTES) throw new Error('Image exceeds 8 MB limit');
    return { buffer, mimetype, source: 'base64' };
  }

  if (!imageUrl) throw new Error('imageUrl or imageBase64 is required');

  let url;
  try { url = new URL(imageUrl); } catch { throw new Error('Invalid image URL'); }
  if (!['http:', 'https:'].includes(url.protocol)) throw new Error('Image URL must use http or https');
  if (isPrivateImageHostname(url.hostname)) throw new Error('Private or local image URLs are not allowed');

  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 15000);
  let response;
  try {
    response = await fetch(url, { redirect: 'follow', signal: controller.signal });
  } finally {
    clearTimeout(timeout);
  }

  if (!response.ok) throw new Error(`Could not download image: HTTP ${response.status}`);
  const contentType = String(response.headers.get('content-type') || '').split(';')[0].trim().toLowerCase();
  if (contentType && !contentType.startsWith('image/')) throw new Error(`URL is not an image (${contentType})`);

  const declaredLength = Number(response.headers.get('content-length') || 0);
  if (declaredLength > MAX_IMAGE_BYTES) throw new Error('Image exceeds 8 MB limit');

  const buffer = Buffer.from(await response.arrayBuffer());
  if (!buffer.length) throw new Error('Downloaded image is empty');
  if (buffer.length > MAX_IMAGE_BYTES) throw new Error('Image exceeds 8 MB limit');

  return {
    buffer,
    mimetype: requestedMime || contentType || 'image/jpeg',
    source: 'url'
  };
}

function isReady() {
  return whatsappState === 'ready' && Boolean(sock);
}

function disconnectStatusCode(error) {
  return error?.output?.statusCode
    ?? error?.data?.statusCode
    ?? error?.statusCode
    ?? null;
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

  currentSock.ev.on('chats.upsert', update => {
    for (const chat of update || []) upsertChat(chat);
  });

  currentSock.ev.on('chats.update', update => {
    for (const chat of update || []) upsertChat(chat);
  });

  currentSock.ev.on('contacts.upsert', update => {
    for (const contact of update || []) upsertContact(contact);
  });

  currentSock.ev.on('contacts.update', update => {
    for (const contact of update || []) upsertContact(contact);
  });

  currentSock.ev.on('messages.upsert', ({ messages }) => {
    for (const message of messages || []) cacheMessage(message);
  });

  currentSock.ev.on('connection.update', async update => {
    if (generation !== socketGeneration) return;

    const { connection, lastDisconnect, qr } = update;

    if (qr) {
      try {
        latestQrDataUrl = await QRCode.toDataURL(qr, {
          width: 560,
          margin: 3,
          errorCorrectionLevel: 'M'
        });
        latestQrAt = new Date().toISOString();
        whatsappState = 'waiting_for_qr_scan';
        lastError = null;
        console.log('[Baileys] QR gráfico pronto em /qr');
      } catch (error) {
        whatsappState = 'qr_error';
        lastError = error.message;
        console.error('[Baileys] Erro ao renderizar QR:', error);
      }
    }

    if (connection === 'connecting' && !qr && whatsappState !== 'waiting_for_qr_scan') {
      whatsappState = 'connecting';
    }

    if (connection === 'open') {
      latestQrDataUrl = null;
      latestQrAt = null;
      whatsappState = 'ready';
      lastError = null;
      me = currentSock.user ? {
        id: currentSock.user.id || null,
        name: currentSock.user.name || null
      } : null;
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
  res.type('html').send(`<!doctype html><html lang="pt-BR"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>WhatsApp Personal Bridge</title></head><body style="font-family:Arial,sans-serif;max-width:760px;margin:40px auto;padding:0 20px"><h1>WhatsApp Personal Bridge · Baileys</h1><p>Serviço ativo sem Chrome/Puppeteer.</p><p><a href="/qr">Abrir QR Code</a></p><p>Status: <strong>${whatsappState}</strong></p></body></html>`);
});

app.get('/health', (_req, res) => {
  res.json({
    ok: true,
    service: 'whatsapp-personal-render-baileys',
    whatsappState,
    ready: isReady(),
    hasQr: Boolean(latestQrDataUrl),
    qrGeneratedAt: latestQrAt,
    cachedChats: chats.size,
    lastError
  });
});

app.get('/qr', (req, res) => {
  if (QR_SECRET && req.query.key !== QR_SECRET) {
    return res.status(401).type('html').send('<h1>401 - chave do QR inválida</h1><p>Use /qr?key=SUA_CHAVE.</p>');
  }

  let content;
  if (latestQrDataUrl) {
    content = `<div class="badge">Aguardando leitura</div><img src="${latestQrDataUrl}" alt="QR Code do WhatsApp" class="qr"><p>WhatsApp → Dispositivos conectados → Conectar dispositivo.</p><p class="muted">Se o QR expirar, a página recebe automaticamente o próximo código.</p>`;
  } else if (whatsappState === 'ready') {
    content = '<div class="ok">✓</div><h2>WhatsApp conectado</h2><p>A sessão do Baileys está ativa.</p>';
  } else {
    content = `<div class="spinner"></div><h2>Gerando QR Code...</h2><p>Conexão atual: <strong>${whatsappState}</strong></p><p class="muted">A página atualiza automaticamente.</p>`;
  }

  res.type('html').send(`<!doctype html>
<html lang="pt-BR"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta http-equiv="refresh" content="4"><title>Conectar WhatsApp</title>
<style>*{box-sizing:border-box}body{margin:0;min-height:100vh;display:grid;place-items:center;background:#eef3f1;font-family:Arial,sans-serif;color:#172b24;padding:24px}.card{width:min(640px,100%);background:#fff;border-radius:24px;padding:32px;text-align:center;box-shadow:0 12px 40px rgba(0,0,0,.1)}.qr{display:block;width:min(560px,100%);height:auto;margin:22px auto;background:#fff;border:1px solid #dce5e1;border-radius:16px;padding:10px}.badge{display:inline-block;padding:8px 14px;background:#fff3cd;color:#725700;border-radius:999px;font-weight:700}.ok{width:84px;height:84px;border-radius:50%;display:grid;place-items:center;margin:20px auto;background:#dff7e8;color:#13763d;font-size:48px;font-weight:bold}.spinner{width:50px;height:50px;border:6px solid #d8e1dd;border-top-color:#25d366;border-radius:50%;margin:24px auto;animation:s 1s linear infinite}@keyframes s{to{transform:rotate(360deg)}}.state,.muted{margin-top:18px;color:#657970;font-size:14px;line-height:1.5}</style></head>
<body><main class="card"><h1>Conectar WhatsApp</h1>${content}<div class="state">Estado: <strong>${whatsappState}</strong>${lastError ? `<br>Erro: ${String(lastError).replace(/</g, '&lt;')}` : ''}</div></main></body></html>`);
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
    res.json({ ok: true, message: 'Sessão apagada. Um novo QR será gerado.' });
  } catch (error) {
    res.status(500).json({ error: error.message });
  }
});

app.get('/openapi.json', (req, res) => {
  const serverUrl = PUBLIC_BASE_URL || `${req.protocol}://${req.get('host')}`;
  res.json({
    openapi: '3.1.0',
    info: {
      title: 'Personal WhatsApp Bridge API',
      version: '2.1.0',
      description: 'Private WhatsApp API powered by Baileys (WebSocket, no browser).'
    },
    servers: [{ url: serverUrl }],
    components: {
      securitySchemes: {
        bearerAuth: { type: 'http', scheme: 'bearer', bearerFormat: 'API token' }
      }
    },
    security: [{ bearerAuth: [] }],
    paths: {
      '/api/status': { get: { operationId: 'getWhatsAppStatus', summary: 'Get WhatsApp connection status', responses: { '200': { description: 'Status' } } } },
      '/api/chats': { get: { operationId: 'listChats', summary: 'List cached WhatsApp chats', parameters: [{ name: 'limit', in: 'query', schema: { type: 'integer', default: 30, maximum: 100 } }], responses: { '200': { description: 'Chats' } } } },
      '/api/chats/{chatId}/messages': { get: { operationId: 'getChatMessages', summary: 'Read cached messages from a chat', parameters: [{ name: 'chatId', in: 'path', required: true, schema: { type: 'string' } }, { name: 'limit', in: 'query', schema: { type: 'integer', default: 30, maximum: 100 } }], responses: { '200': { description: 'Messages' } } } },
      '/api/search': { get: { operationId: 'searchMessages', summary: 'Search cached WhatsApp messages', parameters: [{ name: 'q', in: 'query', required: true, schema: { type: 'string' } }], responses: { '200': { description: 'Search results' } } } },
      '/api/send': { post: { operationId: 'sendWhatsAppMessage', summary: 'Send one WhatsApp text message', requestBody: { required: true, content: { 'application/json': { schema: { type: 'object', required: ['to', 'message'], properties: { to: { type: 'string', description: 'Phone number with country code or a WhatsApp JID.' }, message: { type: 'string', maxLength: 5000 } } } } } }, responses: { '200': { description: 'Message sent' } } } },
      '/api/send-image': { post: { operationId: 'sendWhatsAppImage', summary: 'Send one WhatsApp image from a public URL or base64 payload', requestBody: { required: true, content: { 'application/json': { schema: { type: 'object', required: ['to'], properties: { to: { type: 'string' }, imageUrl: { type: 'string', format: 'uri' }, imageBase64: { type: 'string' }, mimetype: { type: 'string' }, caption: { type: 'string', maxLength: 5000 } } } } } }, responses: { '200': { description: 'Image sent' } } } }
    }
  });
});

app.get('/api/status', requireApiToken, (_req, res) => {
  res.json({
    state: whatsappState,
    ready: isReady(),
    me,
    cachedChats: chats.size,
    authPath: AUTH_PATH,
    lastError
  });
});

app.get('/api/chats', requireApiToken, (req, res) => {
  const limit = Math.min(Math.max(Number(req.query.limit || 30), 1), 100);
  const result = Array.from(chats.values())
    .map(serializeChat)
    .sort((a, b) => (b.timestamp || 0) - (a.timestamp || 0))
    .slice(0, limit);
  res.json({ chats: result });
});

app.get('/api/chats/:chatId/messages', requireApiToken, (req, res) => {
  const limit = Math.min(Math.max(Number(req.query.limit || 30), 1), 100);
  const chatId = req.params.chatId;
  const items = (messagesByChat.get(chatId) || [])
    .slice(-limit)
    .map(serializeMessage);
  res.json({ chatId, messages: items });
});

app.get('/api/search', requireApiToken, (req, res) => {
  const q = String(req.query.q || '').trim().toLowerCase();
  if (!q) return res.status(400).json({ error: 'Query parameter q is required' });

  const results = [];
  for (const [chatId, items] of messagesByChat.entries()) {
    for (const message of items) {
      const serialized = serializeMessage(message);
      if ((serialized.text || '').toLowerCase().includes(q)) {
        results.push({ chatId, message: serialized });
      }
    }
  }

  results.sort((a, b) => (b.message.timestamp || 0) - (a.message.timestamp || 0));
  res.json({ query: q, results: results.slice(0, 100) });
});

app.post('/api/send', requireApiToken, async (req, res) => {
  try {
    if (!isReady()) {
      return res.status(409).json({ error: 'WhatsApp is not ready', state: whatsappState });
    }

    const jid = jidFromDestination(req.body?.to);
    const message = String(req.body?.message || '').trim();

    if (!jid) return res.status(400).json({ error: 'Invalid phone number or JID' });
    if (!message) return res.status(400).json({ error: 'Message is required' });
    if (message.length > 5000) return res.status(400).json({ error: 'Message is too long' });

    const sent = await sock.sendMessage(jid, { text: message });
    if (sent) cacheMessage(sent);

    res.json({
      ok: true,
      id: sent?.key?.id || null,
      to: jid,
      timestamp: toNumber(sent?.messageTimestamp)
    });
  } catch (error) {
    res.status(500).json({ error: error.message });
  }
});

app.post('/api/send-image', requireApiToken, async (req, res) => {
  try {
    if (!isReady()) {
      return res.status(409).json({ error: 'WhatsApp is not ready', state: whatsappState });
    }

    const jid = jidFromDestination(req.body?.to);
    const caption = String(req.body?.caption || '').trim();
    if (!jid) return res.status(400).json({ error: 'Invalid phone number or JID' });
    if (caption.length > 5000) return res.status(400).json({ error: 'Caption is too long' });

    const image = await resolveImageInput(req.body || {});
    const sent = await sock.sendMessage(jid, {
      image: image.buffer,
      mimetype: image.mimetype,
      ...(caption ? { caption } : {})
    });
    if (sent) cacheMessage(sent);

    res.json({
      ok: true,
      id: sent?.key?.id || null,
      to: jid,
      timestamp: toNumber(sent?.messageTimestamp),
      bytes: image.buffer.length,
      mimetype: image.mimetype,
      source: image.source
    });
  } catch (error) {
    const message = error?.name === 'AbortError' ? 'Timed out downloading image' : error.message;
    res.status(400).json({ error: message });
  }
});

const server = app.listen(PORT, '0.0.0.0', () => {
  console.log(`[HTTP] Listening on 0.0.0.0:${PORT}`);
  console.log('[Baileys] Iniciando conexão sem Chrome/Puppeteer...');
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
