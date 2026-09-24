require('dotenv').config();

const express = require('express');
const QRCode = require('qrcode');
const { Client, LocalAuth } = require('whatsapp-web.js');

const app = express();
app.use(express.json({ limit: '1mb' }));

const PORT = Number(process.env.PORT || 10000);
const API_TOKEN = process.env.API_TOKEN || '';
const QR_SECRET = process.env.QR_SECRET || '';
const AUTH_PATH = process.env.WWEBJS_AUTH_PATH || '/tmp/.wwebjs_auth';

let latestQr = null;
let latestQrDataUrl = null;
let whatsappState = 'starting';
let lastError = null;
let me = null;

function requireApiToken(req, res, next) {
  if (!API_TOKEN) {
    return res.status(503).json({ error: 'API_TOKEN is not configured' });
  }

  const auth = req.headers.authorization || '';
  const bearer = auth.startsWith('Bearer ') ? auth.slice(7) : null;
  const apiKey = req.headers['x-api-key'];

  if (bearer !== API_TOKEN && apiKey !== API_TOKEN) {
    return res.status(401).json({ error: 'Unauthorized' });
  }

  next();
}

function normalizeNumber(value) {
  return String(value || '').replace(/\D/g, '');
}

function serializeChat(chat) {
  return {
    id: chat.id?._serialized,
    name: chat.name || null,
    isGroup: Boolean(chat.isGroup),
    unreadCount: chat.unreadCount || 0,
    timestamp: chat.timestamp || null,
    pinned: Boolean(chat.pinned),
    archived: Boolean(chat.archived)
  };
}

function serializeMessage(message) {
  return {
    id: message.id?._serialized,
    from: message.from,
    to: message.to,
    fromMe: Boolean(message.fromMe),
    body: message.body || '',
    timestamp: message.timestamp,
    type: message.type,
    hasMedia: Boolean(message.hasMedia),
    author: message.author || null,
    ack: message.ack
  };
}

const client = new Client({
  authStrategy: new LocalAuth({
    clientId: process.env.WWEBJS_CLIENT_ID || 'personal',
    dataPath: AUTH_PATH
  }),
  puppeteer: {
    headless: true,
    executablePath: process.env.PUPPETEER_EXECUTABLE_PATH || undefined,
    args: [
      '--no-sandbox',
      '--disable-setuid-sandbox',
      '--disable-dev-shm-usage',
      '--disable-gpu',
      '--no-first-run',
      '--no-zygote'
    ]
  }
});

client.on('qr', async (qr) => {
  try {
    latestQr = qr;
    latestQrDataUrl = await QRCode.toDataURL(qr, { width: 420, margin: 2 });
    whatsappState = 'waiting_for_qr_scan';
    lastError = null;
    console.log('[WhatsApp] QR generated. Open /qr to scan.');
  } catch (error) {
    lastError = error.message;
    console.error('[WhatsApp] Failed to render QR:', error);
  }
});

client.on('authenticated', () => {
  whatsappState = 'authenticated';
  latestQr = null;
  latestQrDataUrl = null;
  console.log('[WhatsApp] Authenticated.');
});

client.on('ready', async () => {
  whatsappState = 'ready';
  latestQr = null;
  latestQrDataUrl = null;
  lastError = null;
  try {
    me = client.info ? {
      wid: client.info.wid?._serialized,
      pushname: client.info.pushname || null,
      platform: client.info.platform || null
    } : null;
  } catch (_) {
    me = null;
  }
  console.log('[WhatsApp] Client ready.');
});

client.on('auth_failure', (message) => {
  whatsappState = 'auth_failure';
  lastError = String(message || 'Authentication failed');
  console.error('[WhatsApp] Authentication failure:', message);
});

client.on('disconnected', (reason) => {
  whatsappState = 'disconnected';
  lastError = String(reason || 'Disconnected');
  me = null;
  console.error('[WhatsApp] Disconnected:', reason);
});

client.on('change_state', (state) => {
  console.log('[WhatsApp] State:', state);
});

app.get('/', (_req, res) => {
  res.type('html').send(`<!doctype html>
<html lang="pt-BR">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>WhatsApp Personal Bridge</title></head>
<body style="font-family:Arial,sans-serif;max-width:760px;margin:40px auto;padding:0 20px">
  <h1>WhatsApp Personal Bridge</h1>
  <p>Serviço ativo. Use <code>/health</code> para saúde e <code>/qr?key=SUA_CHAVE</code> para autenticar o WhatsApp.</p>
  <p>Status atual: <strong>${whatsappState}</strong></p>
</body></html>`);
});

app.get('/health', (_req, res) => {
  res.json({
    ok: true,
    service: 'whatsapp-personal-render',
    whatsappState,
    ready: whatsappState === 'ready',
    lastError
  });
});

app.get('/qr', (req, res) => {
  if (!QR_SECRET || req.query.key !== QR_SECRET) {
    return res.status(401).type('html').send('<h1>401 - chave do QR inválida</h1>');
  }

  const qrBlock = latestQrDataUrl
    ? `<img src="${latestQrDataUrl}" alt="QR Code do WhatsApp" style="width:min(420px,90vw);height:auto;border:1px solid #ddd;border-radius:16px;padding:12px;background:#fff">`
    : whatsappState === 'ready'
      ? '<div style="font-size:64px">✅</div><h2>WhatsApp conectado</h2>'
      : '<div class="spinner"></div><h2>Aguardando um QR Code...</h2>';

  res.type('html').send(`<!doctype html>
<html lang="pt-BR">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <meta http-equiv="refresh" content="5">
  <title>Conectar WhatsApp</title>
  <style>
    body{margin:0;background:#f5f7f8;font-family:Arial,sans-serif;color:#172b24;display:grid;place-items:center;min-height:100vh}
    main{background:white;border-radius:24px;padding:32px;text-align:center;box-shadow:0 10px 35px rgba(0,0,0,.08);width:min(520px,calc(100vw - 48px))}
    code{background:#eef3f1;padding:4px 8px;border-radius:6px}.state{margin-top:18px;color:#587068}
    .spinner{width:42px;height:42px;border:5px solid #ddd;border-top-color:#222;border-radius:50%;margin:24px auto;animation:s 1s linear infinite}@keyframes s{to{transform:rotate(360deg)}}
  </style>
</head>
<body><main>
  <h1>Conectar WhatsApp</h1>
  <p>No celular: WhatsApp → Dispositivos conectados → Conectar dispositivo.</p>
  ${qrBlock}
  <p class="state">Estado: <code>${whatsappState}</code></p>
  <small>A página atualiza automaticamente a cada 5 segundos.</small>
</main></body></html>`);
});

app.get('/openapi.json', (req, res) => {
  const serverUrl = process.env.PUBLIC_BASE_URL || `${req.protocol}://${req.get('host')}`;
  res.json({
    openapi: '3.1.0',
    info: {
      title: 'Personal WhatsApp Bridge API',
      version: '1.0.0',
      description: 'Private API for reading and sending messages through a personal WhatsApp Web session.'
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
      '/api/chats': { get: { operationId: 'listChats', summary: 'List recent WhatsApp chats', parameters: [{ name: 'limit', in: 'query', schema: { type: 'integer', default: 30, maximum: 100 } }], responses: { '200': { description: 'Chats' } } } },
      '/api/chats/{chatId}/messages': { get: { operationId: 'getChatMessages', summary: 'Read messages from a chat', parameters: [{ name: 'chatId', in: 'path', required: true, schema: { type: 'string' } }, { name: 'limit', in: 'query', schema: { type: 'integer', default: 30, maximum: 100 } }], responses: { '200': { description: 'Messages' } } } },
      '/api/search': { get: { operationId: 'searchMessages', summary: 'Search recent WhatsApp messages', parameters: [{ name: 'q', in: 'query', required: true, schema: { type: 'string' } }, { name: 'chatLimit', in: 'query', schema: { type: 'integer', default: 30, maximum: 100 } }, { name: 'messagesPerChat', in: 'query', schema: { type: 'integer', default: 50, maximum: 100 } }], responses: { '200': { description: 'Search results' } } } },
      '/api/send': { post: { operationId: 'sendWhatsAppMessage', summary: 'Send a WhatsApp message to one phone number', requestBody: { required: true, content: { 'application/json': { schema: { type: 'object', required: ['to', 'message'], properties: { to: { type: 'string', description: 'Phone number with country code, digits only or formatted.' }, message: { type: 'string', maxLength: 5000 } } } } } }, responses: { '200': { description: 'Message sent' } } } }
    }
  });
});

app.get('/api/status', requireApiToken, (_req, res) => {
  res.json({ state: whatsappState, ready: whatsappState === 'ready', me, lastError });
});

app.get('/api/chats', requireApiToken, async (req, res) => {
  try {
    if (whatsappState !== 'ready') return res.status(409).json({ error: 'WhatsApp is not ready', state: whatsappState });
    const limit = Math.min(Math.max(Number(req.query.limit || 30), 1), 100);
    const chats = await client.getChats();
    res.json({ chats: chats.slice(0, limit).map(serializeChat) });
  } catch (error) {
    res.status(500).json({ error: error.message });
  }
});

app.get('/api/chats/:chatId/messages', requireApiToken, async (req, res) => {
  try {
    if (whatsappState !== 'ready') return res.status(409).json({ error: 'WhatsApp is not ready', state: whatsappState });
    const limit = Math.min(Math.max(Number(req.query.limit || 30), 1), 100);
    const chat = await client.getChatById(req.params.chatId);
    const messages = await chat.fetchMessages({ limit });
    res.json({ chat: serializeChat(chat), messages: messages.map(serializeMessage) });
  } catch (error) {
    res.status(500).json({ error: error.message });
  }
});

app.get('/api/search', requireApiToken, async (req, res) => {
  try {
    if (whatsappState !== 'ready') return res.status(409).json({ error: 'WhatsApp is not ready', state: whatsappState });
    const q = String(req.query.q || '').trim().toLowerCase();
    if (!q) return res.status(400).json({ error: 'Query parameter q is required' });

    const chatLimit = Math.min(Math.max(Number(req.query.chatLimit || 30), 1), 100);
    const messagesPerChat = Math.min(Math.max(Number(req.query.messagesPerChat || 50), 1), 100);
    const chats = (await client.getChats()).slice(0, chatLimit);
    const results = [];

    for (const chat of chats) {
      const messages = await chat.fetchMessages({ limit: messagesPerChat });
      for (const message of messages) {
        if ((message.body || '').toLowerCase().includes(q)) {
          results.push({ chat: serializeChat(chat), message: serializeMessage(message) });
        }
      }
    }

    results.sort((a, b) => (b.message.timestamp || 0) - (a.message.timestamp || 0));
    res.json({ query: q, results: results.slice(0, 100) });
  } catch (error) {
    res.status(500).json({ error: error.message });
  }
});

app.post('/api/send', requireApiToken, async (req, res) => {
  try {
    if (whatsappState !== 'ready') return res.status(409).json({ error: 'WhatsApp is not ready', state: whatsappState });
    const number = normalizeNumber(req.body?.to);
    const message = String(req.body?.message || '').trim();

    if (!number || number.length < 10) return res.status(400).json({ error: 'Invalid phone number' });
    if (!message) return res.status(400).json({ error: 'Message is required' });
    if (message.length > 5000) return res.status(400).json({ error: 'Message is too long' });

    const chatId = `${number}@c.us`;
    const registered = await client.isRegisteredUser(chatId);
    if (!registered) return res.status(404).json({ error: 'Number is not registered on WhatsApp' });

    const sent = await client.sendMessage(chatId, message);
    res.json({ ok: true, id: sent.id?._serialized, to: number, timestamp: sent.timestamp });
  } catch (error) {
    res.status(500).json({ error: error.message });
  }
});

app.listen(PORT, '0.0.0.0', () => {
  console.log(`[HTTP] Listening on 0.0.0.0:${PORT}`);
  client.initialize().catch((error) => {
    whatsappState = 'initialization_error';
    lastError = error.message;
    console.error('[WhatsApp] Initialization error:', error);
  });
});

async function shutdown(signal) {
  console.log(`[System] ${signal} received, shutting down.`);
  try { await client.destroy(); } catch (_) {}
  process.exit(0);
}

process.on('SIGTERM', () => shutdown('SIGTERM'));
process.on('SIGINT', () => shutdown('SIGINT'));
