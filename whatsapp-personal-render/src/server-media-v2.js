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
import { createPersistentStore } from './persistent-store.js';
import { whatsappEvents } from './whatsapp-events.js';

const app = express();
app.use(express.json({ limit: '20mb' }));

const PORT = Number(process.env.PORT || 10000);
const API_TOKEN = process.env.API_TOKEN || '';
const QR_SECRET = process.env.QR_SECRET || '';
const AUTH_PATH = process.env.BAILEYS_AUTH_PATH || path.resolve(process.cwd(), '.baileys_auth');
const DB_PATH = process.env.WHATSAPP_DB_PATH || (
  AUTH_PATH.startsWith('/data/') ? '/data/whatsapp.sqlite'
    : AUTH_PATH.startsWith('/var/data/') ? '/var/data/whatsapp.sqlite'
      : path.resolve(process.cwd(), 'whatsapp.sqlite')
);
const logger = pino({ level: process.env.BAILEYS_LOG_LEVEL || 'silent' });

const MAX_MESSAGES_PER_CHAT = 300;
const MAX_IMAGE_INPUT_BYTES = 12 * 1024 * 1024;
const MAX_AUDIO_BYTES = 12 * 1024 * 1024;

let sock = null;
let whatsappState = 'starting';
let lastError = null;
let latestQrDataUrl = null;
let latestQrAt = null;
let latestPairingCode = null;
let latestPairingAt = null;
let latestPairingPhoneLast4 = null;
let pairingModeActive = false;
let authRegistered = false;
let me = null;
let reconnectTimer = null;
let socketGeneration = 0;

const chats = new Map();
const contacts = new Map();
const messagesByChat = new Map();
const persistentStore = createPersistentStore(DB_PATH);

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
    remoteJidAlt: message?.key?.remoteJidAlt || null,
    participant: message?.key?.participant || null,
    participantAlt: message?.key?.participantAlt || null,
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
    timestamp: toNumber(chat?.conversationTimestamp ?? chat?.timestamp),
    archived: Boolean(chat?.archived),
    pinned: Boolean(chat?.pinned)
  };
}

function upsertChat(chat) {
  const id = chat?.id || chat?.jid;
  if (!id) return;
  const merged = { ...(chats.get(id) || {}), ...chat, id };
  chats.set(id, merged);
  persistentStore.upsertChat(serializeChat(merged));
}

function upsertContact(contact) {
  const id = contact?.id;
  if (!id) return;
  const merged = { ...(contacts.get(id) || {}), ...contact };
  contacts.set(id, merged);
  persistentStore.upsertContact(merged);
}

function cacheMessage(message) {
  const jid = message?.key?.remoteJid;
  if (!jid) return null;
  const current = messagesByChat.get(jid) || [];
  const id = message?.key?.id;
  const withoutDuplicate = id ? current.filter(item => item?.key?.id !== id) : current;
  withoutDuplicate.push(message);
  withoutDuplicate.sort((a, b) => (toNumber(a?.messageTimestamp) || 0) - (toNumber(b?.messageTimestamp) || 0));
  if (withoutDuplicate.length > MAX_MESSAGES_PER_CHAT) {
    withoutDuplicate.splice(0, withoutDuplicate.length - MAX_MESSAGES_PER_CHAT);
  }
  messagesByChat.set(jid, withoutDuplicate);

  const serialized = serializeMessage(message);
  persistentStore.inferAndStoreMappings(message?.key);
  persistentStore.upsertMessage(serialized, message);
  upsertChat({ id: jid, conversationTimestamp: serialized.timestamp || Math.floor(Date.now() / 1000) });
  return serialized;
}

function findMessage(chatId, messageId) {
  const items = messagesByChat.get(chatId) || [];
  return items.find(item => item?.key?.id === messageId) || null;
}

function findQuotedMessage(chatId, messageId) {
  return findMessage(chatId, messageId) || persistentStore.getQuotedMessage(chatId, messageId);
}

function findMessageAuthor(chatId, messageId) {
  const raw = findMessage(chatId, messageId);
  if (raw) return raw?.key?.participant || raw?.key?.participantAlt || raw?.key?.remoteJidAlt || raw?.key?.remoteJid || null;
  return persistentStore.getMessageAuthor(chatId, messageId);
}

function jidFromDestination(value) {
  const raw = String(value || '').trim();
  if (!raw) return null;
  if (raw.includes('@')) return raw;
  const digits = raw.replace(/\D/g, '');
  if (digits.length < 10) return null;
  return `${digits}@s.whatsapp.net`;
}

function normalizePairingPhone(value) {
  let digits = String(value || '').replace(/\D/g, '');
  if (!digits) return null;
  if ((digits.length === 10 || digits.length === 11) && !digits.startsWith('55')) digits = `55${digits}`;
  if (digits.length < 10 || digits.length > 15) return null;
  return digits;
}

function formatPairingCode(value) {
  const code = String(value || '').replace(/[^a-zA-Z0-9]/g, '').toUpperCase();
  if (code.length === 8) return `${code.slice(0, 4)}-${code.slice(4)}`;
  return code;
}

function clearPairingState() {
  latestPairingCode = null;
  latestPairingAt = null;
  latestPairingPhoneLast4 = null;
  pairingModeActive = false;
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
    authRegistered = false;
    clearPairingState();
    console.log('[Baileys] Sessão inválida removida; novo QR/código será solicitado.');
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
  authRegistered = Boolean(state.creds?.registered);

  const currentSock = makeWASocket({
    auth: state,
    logger,
    printQRInTerminal: false,
    markOnlineOnConnect: false,
    emitOwnEvents: true,
    syncFullHistory: true,
    generateHighQualityLinkPreview: false
  });

  sock = currentSock;
  console.log(`[Baileys] Socket iniciado. Auth: ${AUTH_PATH}`);
  currentSock.ev.on('creds.update', async () => {
    authRegistered = Boolean(state.creds?.registered);
    await saveCreds();
  });

  currentSock.ev.on('messaging-history.set', ({ chats: historyChats, contacts: historyContacts, messages, lidPnMappings }) => {
    for (const chat of historyChats || []) upsertChat(chat);
    for (const contact of historyContacts || []) upsertContact(contact);
    for (const mapping of lidPnMappings || []) persistentStore.upsertLidMapping(mapping?.lid, mapping?.pn);
    for (const message of messages || []) cacheMessage(message);
    console.log(`[Baileys] Histórico: ${historyChats?.length || 0} chats, ${messages?.length || 0} mensagens.`);
  });
  currentSock.ev.on('chats.upsert', update => { for (const chat of update || []) upsertChat(chat); });
  currentSock.ev.on('chats.update', update => { for (const chat of update || []) upsertChat(chat); });
  currentSock.ev.on('contacts.upsert', update => { for (const contact of update || []) upsertContact(contact); });
  currentSock.ev.on('contacts.update', update => { for (const contact of update || []) upsertContact(contact); });
  currentSock.ev.on('lid-mapping.update', mapping => {
    if (mapping) persistentStore.upsertLidMapping(mapping.lid, mapping.pn);
  });
  currentSock.ev.on('messages.upsert', ({ messages, type }) => {
    for (const message of messages || []) {
      const serialized = cacheMessage(message);
      if (!serialized?.id || !serialized?.chatId) continue;
      const chat = serializeChat(chats.get(serialized.chatId) || {
        id: serialized.chatId,
        name: contactName(serialized.chatId),
        conversationTimestamp: serialized.timestamp
      });
      whatsappEvents.emit('message', { message: serialized, chat, upsertType: type || null });
    }
  });

  currentSock.ev.on('connection.update', async update => {
    if (generation !== socketGeneration) return;
    const { connection, lastDisconnect, qr } = update;

    if (qr) {
      try {
        latestQrDataUrl = await QRCode.toDataURL(qr, { width: 560, margin: 3, errorCorrectionLevel: 'M' });
        latestQrAt = new Date().toISOString();
        if (!pairingModeActive) whatsappState = 'waiting_for_qr_scan';
        lastError = null;
        console.log('[Baileys] QR gráfico pronto em /qr');
      } catch (error) {
        whatsappState = 'qr_error';
        lastError = error.message;
      }
    }

    if (connection === 'connecting' && !qr && whatsappState !== 'waiting_for_qr_scan' && whatsappState !== 'waiting_for_pairing_code') whatsappState = 'connecting';

    if (connection === 'open') {
      latestQrDataUrl = null;
      latestQrAt = null;
      clearPairingState();
      authRegistered = true;
      whatsappState = 'ready';
      lastError = null;
      me = currentSock.user ? { id: currentSock.user.id || null, name: currentSock.user.name || null } : null;
      console.log('[Baileys] WhatsApp conectado.');
      whatsappEvents.emit('ready', { me });
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
        authRegistered = false;
        clearPairingState();
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

function normalizeMentions(values) {
  const input = Array.isArray(values) ? values : [];
  const unique = new Set();
  for (const value of input) {
    const jid = jidFromDestination(value);
    if (!jid) continue;
    unique.add(persistentStore.resolvePreferredJid(jid));
  }
  return Array.from(unique);
}

function withVisibleMentionPrefix(message, mentions, enabled = true) {
  if (!enabled || !mentions.length) return message;
  const missing = mentions
    .map(jid => String(jid).split('@')[0])
    .filter(number => number && !message.includes(`@${number}`));
  if (!missing.length) return message;
  return `${missing.map(number => `@${number}`).join(' ')} ${message}`.trim();
}

app.get('/', (_req, res) => {
  res.type('html').send(`<!doctype html><html lang="pt-BR"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>WhatsApp Personal Bridge</title></head><body style="font-family:Arial,sans-serif;max-width:760px;margin:40px auto;padding:0 20px"><h1>WhatsApp Personal Bridge · Media v2</h1><p>Status: <strong>${whatsappState}</strong></p><p><a href="/qr">Conectar WhatsApp por QR Code ou código</a></p></body></html>`);
});

app.get('/health', (_req, res) => {
  res.json({
    ok: true,
    service: 'whatsapp-personal-render-media-v2',
    whatsappState,
    ready: isReady(),
    hasQr: Boolean(latestQrDataUrl),
    qrGeneratedAt: latestQrAt,
    pairingMode: pairingModeActive,
    pairingCodeGeneratedAt: latestPairingAt,
    cachedChats: chats.size,
    authPath: AUTH_PATH,
    database: persistentStore.stats(),
    lastError
  });
});

app.get('/qr', (req, res) => {
  if (QR_SECRET && req.query.key !== QR_SECRET) return res.status(401).type('html').send('<h1>401 - chave do QR inválida</h1>');

  const keySuffix = req.query.key ? `?key=${encodeURIComponent(String(req.query.key))}` : '';
  const pairingInfo = latestPairingCode
    ? `<div class="pairing-result"><div class="pairing-label">Código para inserir no WhatsApp</div><div class="pairing-code">${latestPairingCode}</div><p>Abra o WhatsApp no celular → Dispositivos conectados → Conectar dispositivo → <strong>Conectar com número de telefone</strong> e digite este código.</p>${latestPairingPhoneLast4 ? `<p class="muted">Número final: ••••${latestPairingPhoneLast4}</p>` : ''}</div>`
    : '';

  let content;
  if (whatsappState === 'ready') {
    content = '<div class="connected"><div class="check">✓</div><h2>WhatsApp conectado</h2><p>A sessão está pronta para uso pelo MeuWhats.</p></div>';
  } else {
    const qrBlock = latestQrDataUrl
      ? `<img src="${latestQrDataUrl}" alt="QR Code do WhatsApp" class="qr"><p>WhatsApp → Dispositivos conectados → Conectar dispositivo e leia o QR Code.</p>`
      : `<div class="spinner"></div><h3>Gerando QR Code...</h3><p class="muted">Estado: ${whatsappState}</p>`;

    content = `
      <div class="methods">
        <section class="method">
          <div class="method-number">1</div>
          <h2>Conectar por QR Code</h2>
          ${qrBlock}
        </section>
        <div class="or"><span>ou</span></div>
        <section class="method">
          <div class="method-number">2</div>
          <h2>Conectar com código</h2>
          <p>Use esta opção quando estiver no celular e não puder escanear o QR.</p>
          <form id="pairing-form" class="pairing-form">
            <label for="phone">Seu número do WhatsApp</label>
            <input id="phone" name="phone" inputmode="tel" autocomplete="tel" placeholder="+55 41 99999-9999" required>
            <button type="submit">Gerar código</button>
          </form>
          <div id="pairing-message" class="message" aria-live="polite"></div>
          ${pairingInfo}
        </section>
      </div>`;
  }

  res.type('html').send(`<!doctype html>
<html lang="pt-BR">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Conectar WhatsApp</title>
<style>
*{box-sizing:border-box}body{margin:0;min-height:100vh;background:#0b141a;color:#e9edef;font-family:Inter,Arial,sans-serif;padding:24px}.page{width:min(980px,100%);margin:0 auto}.hero{text-align:center;margin:12px 0 24px}.hero h1{font-size:clamp(28px,5vw,42px);margin:0 0 8px}.hero p{color:#aebac1;margin:0}.card{background:#111b21;border:1px solid #22313a;border-radius:24px;padding:clamp(18px,4vw,32px);box-shadow:0 18px 60px rgba(0,0,0,.28)}.methods{display:grid;grid-template-columns:1fr auto 1fr;gap:24px;align-items:start}.method{text-align:center;min-width:0}.method h2{font-size:22px;margin:6px 0 10px}.method p{color:#aebac1;line-height:1.55}.method-number{width:34px;height:34px;border-radius:50%;display:grid;place-items:center;margin:0 auto 8px;background:#00a884;color:#071b16;font-weight:800}.or{align-self:center;color:#8696a0}.or span{display:grid;place-items:center;width:42px;height:42px;border-radius:50%;background:#202c33;font-size:13px;font-weight:700}.qr{display:block;width:min(390px,100%);height:auto;margin:18px auto;background:#fff;border-radius:16px;padding:10px}.pairing-form{display:grid;gap:10px;margin:20px auto 10px;max-width:390px;text-align:left}.pairing-form label{font-size:13px;color:#c7d0d5}.pairing-form input{width:100%;border:1px solid #3b4a54;background:#202c33;color:#fff;border-radius:12px;padding:14px 15px;font-size:16px;outline:none}.pairing-form input:focus{border-color:#00a884}.pairing-form button,.reset button{border:0;border-radius:999px;padding:13px 18px;background:#00a884;color:#071b16;font-weight:800;font-size:15px;cursor:pointer}.pairing-form button:disabled{opacity:.6;cursor:wait}.pairing-result{margin:18px auto 0;max-width:440px;padding:18px;background:#0d2f29;border:1px solid #1e5d50;border-radius:16px}.pairing-label{font-size:13px;color:#aebac1}.pairing-code{font-size:clamp(32px,8vw,50px);font-weight:900;letter-spacing:.1em;margin:8px 0 12px;color:#25d366;word-break:break-word}.message{min-height:22px;color:#f5c26b;font-size:14px}.muted{color:#8696a0!important;font-size:13px}.connected{text-align:center;padding:30px}.check{width:82px;height:82px;border-radius:50%;display:grid;place-items:center;margin:0 auto 16px;background:#0d5c47;color:#25d366;font-size:48px;font-weight:900}.spinner{width:48px;height:48px;border:5px solid #273942;border-top-color:#25d366;border-radius:50%;margin:26px auto;animation:spin 1s linear infinite}@keyframes spin{to{transform:rotate(360deg)}}.state{text-align:center;margin-top:18px;color:#8696a0;font-size:13px}.reset{text-align:center;margin-top:18px}.reset button{background:#202c33;color:#e9edef;border:1px solid #3b4a54}.error{color:#ff8a8a}@media(max-width:760px){.methods{grid-template-columns:1fr}.or{justify-self:center}.card{border-radius:18px}.qr{width:min(340px,100%)}}
</style>
</head>
<body>
<main class="page">
  <div class="hero"><h1>Conectar WhatsApp</h1><p>Escolha QR Code ou código pelo número de telefone.</p></div>
  <div class="card">${content}<div class="state">Estado: <strong>${whatsappState}</strong>${lastError ? `<br><span class="error">${String(lastError).replace(/[<>&]/g, '')}</span>` : ''}</div></div>
  ${whatsappState !== 'ready' ? `<form class="reset" method="post" action="/qr/reset${keySuffix}"><button type="submit">Gerar nova sessão</button></form>` : ''}
</main>
<script>
(() => {
  const form = document.getElementById('pairing-form');
  const message = document.getElementById('pairing-message');
  if (!form || !message) return;
  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    const phone = String(new FormData(form).get('phone') || '').trim();
    const button = form.querySelector('button');
    button.disabled = true;
    message.className = 'message';
    message.textContent = 'Gerando código...';
    try {
      const response = await fetch('/pairing-code${keySuffix}', {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ phone })
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(data.error || 'Não foi possível gerar o código.');
      message.textContent = 'Código gerado. Atualizando a tela...';
      window.setTimeout(() => window.location.reload(), 250);
    } catch (error) {
      message.className = 'message error';
      message.textContent = error.message || 'Falha ao gerar o código.';
      button.disabled = false;
    }
  });
})();
</script>
${whatsappState !== 'ready' && !latestPairingCode ? '<script>setTimeout(() => location.reload(), 5000)</script>' : ''}
</body></html>`);
});

app.post('/pairing-code', requireQrSecret, async (req, res) => {
  try {
    if (isReady()) return res.status(409).json({ error: 'O WhatsApp já está conectado.' });
    if (!sock || typeof sock.requestPairingCode !== 'function') {
      return res.status(503).json({ error: 'O conector ainda está iniciando. Aguarde alguns segundos e tente novamente.' });
    }
    if (authRegistered) {
      return res.status(409).json({ error: 'Esta sessão já possui credenciais. Use “Gerar nova sessão” antes de vincular outro número.' });
    }
    if (!latestQrAt && whatsappState !== 'waiting_for_qr_scan') {
      return res.status(409).json({ error: 'Aguarde o QR Code aparecer antes de solicitar o código pelo telefone.' });
    }

    const phone = normalizePairingPhone(req.body?.phone);
    if (!phone) return res.status(400).json({ error: 'Informe um número válido com DDI. Ex.: +55 41 99999-9999.' });

    pairingModeActive = true;
    latestPairingCode = null;
    latestPairingAt = null;
    latestPairingPhoneLast4 = phone.slice(-4);
    whatsappState = 'generating_pairing_code';
    lastError = null;

    const code = await sock.requestPairingCode(phone);
    latestPairingCode = formatPairingCode(code);
    latestPairingAt = new Date().toISOString();
    whatsappState = 'waiting_for_pairing_code';
    console.log(`[Baileys] Código de pareamento gerado para final ${latestPairingPhoneLast4}.`);

    res.json({
      ok: true,
      code: latestPairingCode,
      generatedAt: latestPairingAt,
      phoneLast4: latestPairingPhoneLast4,
      state: whatsappState
    });
  } catch (error) {
    pairingModeActive = false;
    latestPairingCode = null;
    latestPairingAt = null;
    whatsappState = latestQrDataUrl ? 'waiting_for_qr_scan' : 'pairing_code_error';
    lastError = error?.message || 'Falha ao gerar código de pareamento';
    console.error('[Baileys] Erro ao gerar código de pareamento:', error);
    res.status(500).json({ error: lastError });
  }
});

app.post('/qr/reset', requireQrSecret, async (_req, res) => {
  try {
    socketGeneration += 1;
    try { sock?.end?.(new Error('manual session reset')); } catch (_) {}
    sock = null;
    latestQrDataUrl = null;
    latestQrAt = null;
    clearPairingState();
    authRegistered = false;
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
  res.json({
    state: whatsappState,
    ready: isReady(),
    me,
    cachedChats: chats.size,
    authPath: AUTH_PATH,
    database: persistentStore.stats(),
    pairingMode: pairingModeActive,
    pairingCodeGeneratedAt: latestPairingAt,
    lastError
  });
});

app.get('/api/chats', requireApiToken, (req, res) => {
  const limit = Math.min(Math.max(Number(req.query.limit || 30), 1), 100);
  const merged = new Map(persistentStore.listChats(300).map(chat => [chat.id, chat]));
  for (const chat of chats.values()) {
    const serialized = serializeChat(chat);
    merged.set(serialized.id, { ...(merged.get(serialized.id) || {}), ...serialized });
  }
  const result = Array.from(merged.values()).sort((a, b) => (b.timestamp || 0) - (a.timestamp || 0)).slice(0, limit);
  res.json({ chats: result });
});

app.get('/api/chats/:chatId/messages', requireApiToken, (req, res) => {
  const limit = Math.min(Math.max(Number(req.query.limit || 30), 1), 100);
  const items = persistentStore.listMessages(req.params.chatId, limit);
  res.json({ chatId: req.params.chatId, messages: items });
});

app.get('/api/search', requireApiToken, (req, res) => {
  const q = String(req.query.q || '').trim();
  if (!q) return res.status(400).json({ error: 'Query parameter q is required' });
  res.json({ query: q, results: persistentStore.searchMessages(q, 100) });
});

app.get('/api/db-stats', requireApiToken, (_req, res) => {
  res.json({ ok: true, ...persistentStore.stats() });
});

app.post('/api/send', requireApiToken, async (req, res) => {
  const startedAt = performance.now();
  try {
    if (!isReady()) return res.status(409).json({ error: 'WhatsApp is not ready', state: whatsappState });
    const jid = jidFromDestination(req.body?.to);
    let message = String(req.body?.message || '').trim();
    if (!jid) return res.status(400).json({ error: 'Invalid phone number or JID' });
    if (!message) return res.status(400).json({ error: 'Message is required' });
    if (message.length > 5000) return res.status(400).json({ error: 'Message is too long' });

    const replyToMessageId = String(req.body?.replyToMessageId || '').trim();
    const mentionAuthorOfMessageId = String(req.body?.mentionAuthorOfMessageId || '').trim();
    const mentions = normalizeMentions(req.body?.mentionJids);

    let quoted = null;
    if (replyToMessageId) {
      quoted = findQuotedMessage(jid, replyToMessageId);
      if (!quoted) return res.status(404).json({ error: 'Message to reply to was not found in cache or persistent history' });
    }

    if (mentionAuthorOfMessageId) {
      const author = findMessageAuthor(jid, mentionAuthorOfMessageId);
      if (!author) return res.status(404).json({ error: 'Message author was not found' });
      const resolved = persistentStore.resolvePreferredJid(author);
      if (resolved && !mentions.includes(resolved)) mentions.push(resolved);
    }

    message = withVisibleMentionPrefix(message, mentions, req.body?.prependMentions !== false);
    const content = { text: message, ...(mentions.length ? { mentions } : {}) };
    const sent = await sock.sendMessage(jid, content, quoted ? { quoted } : undefined);
    if (sent) cacheMessage(sent);
    const latencyMs = Math.round(performance.now() - startedAt);
    console.log(`[Send] text to=${jid} latency=${latencyMs}ms reply=${Boolean(quoted)} mentions=${mentions.length}`);
    res.json({ ok: true, id: sent?.key?.id || null, to: jid, timestamp: toNumber(sent?.messageTimestamp), replyToMessageId: replyToMessageId || null, mentions, latencyMs });
  } catch (error) {
    res.status(500).json({ error: error.message });
  }
});

app.post('/api/react', requireApiToken, async (req, res) => {
  const startedAt = performance.now();
  try {
    if (!isReady()) return res.status(409).json({ error: 'WhatsApp is not ready', state: whatsappState });
    const jid = jidFromDestination(req.body?.to);
    const messageId = String(req.body?.messageId || '').trim();
    const emoji = String(req.body?.emoji ?? '').trim();
    if (!jid) return res.status(400).json({ error: 'Invalid phone number or JID' });
    if (!messageId) return res.status(400).json({ error: 'messageId is required' });
    if (emoji.length > 20) return res.status(400).json({ error: 'Reaction is too long' });
    const target = findQuotedMessage(jid, messageId);
    if (!target?.key) return res.status(404).json({ error: 'Message to react to was not found' });
    const sent = await sock.sendMessage(jid, { react: { text: emoji, key: target.key } });
    if (sent) cacheMessage(sent);
    const latencyMs = Math.round(performance.now() - startedAt);
    console.log(`[Send] reaction to=${jid} message=${messageId} latency=${latencyMs}ms`);
    res.json({ ok: true, to: jid, messageId, emoji, latencyMs });
  } catch (error) {
    res.status(500).json({ error: error.message });
  }
});

app.post('/api/send-image', requireApiToken, async (req, res) => {
  try {
    if (!isReady()) return res.status(409).json({ error: 'WhatsApp is not ready', state: whatsappState });
    const jid = jidFromDestination(req.body?.to);
    let caption = String(req.body?.caption || '').trim();
    if (!jid) return res.status(400).json({ error: 'Invalid phone number or JID' });
    if (caption.length > 5000) return res.status(400).json({ error: 'Caption is too long' });
    const image = await resolveImageInput(req.body || {});
    const replyToMessageId = String(req.body?.replyToMessageId || '').trim();
    const mentions = normalizeMentions(req.body?.mentionJids);
    const quoted = replyToMessageId ? findQuotedMessage(jid, replyToMessageId) : null;
    if (replyToMessageId && !quoted) return res.status(404).json({ error: 'Message to reply to was not found' });
    caption = withVisibleMentionPrefix(caption, mentions, req.body?.prependMentions !== false);
    const sent = await sock.sendMessage(jid, {
      image: image.buffer,
      mimetype: 'image/jpeg',
      ...(caption ? { caption } : {}),
      ...(mentions.length ? { mentions } : {})
    }, quoted ? { quoted } : undefined);
    if (sent) cacheMessage(sent);
    res.json({ ok: true, id: sent?.key?.id || null, to: jid, timestamp: toNumber(sent?.messageTimestamp), bytes: image.buffer.length, mimetype: 'image/jpeg', source: image.source, normalized: true, replyToMessageId: replyToMessageId || null, mentions });
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
    if (!message) return res.status(404).json({ error: 'Audio message is not present in the in-memory media cache; text history remains persisted' });
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
  console.log(`[SQLite] Persistent history: ${persistentStore.path}`);
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
  try { persistentStore.close(); } catch (_) {}
  server.close(() => process.exit(0));
  setTimeout(() => process.exit(0), 3000).unref();
}

process.on('SIGTERM', () => shutdown('SIGTERM'));
process.on('SIGINT', () => shutdown('SIGINT'));
