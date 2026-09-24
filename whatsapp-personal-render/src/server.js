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

let latestQrDataUrl = null;
let whatsappState = 'starting';
let lastError = null;
let me = null;

function requireApiToken(req, res, next) {
  if (!API_TOKEN) return res.status(503).json({ error: 'API_TOKEN is not configured' });
  const auth = req.headers.authorization || '';
  const bearer = auth.startsWith('Bearer ') ? auth.slice(7) : null;
  const apiKey = req.headers['x-api-key'];
  if (bearer !== API_TOKEN && apiKey !== API_TOKEN) return res.status(401).json({ error: 'Unauthorized' });
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
    args: ['--no-sandbox', '--disable-setuid-sandbox', '--disable-dev-shm-usage', '--disable-gpu', '--no-first-run', '--no-zygote']
  }
});

client.on('qr', async (qr) => {
  try {
    latestQrDataUrl = await QRCode.toDataURL(qr, {
      width: 520,
      margin: 3,
      errorCorrectionLevel: 'M'
    });
    whatsappState = 'waiting_for_qr_scan';
    lastError = null;
    console.log('[WhatsApp] QR gráfico pronto em /qr');
  } catch (error) {
    lastError = error.message;
    console.error('[WhatsApp] Erro ao gerar QR:', error);
  }
});

client.on('authenticated', () => {
  whatsappState = 'authenticated';
  latestQrDataUrl = null;
  console.log('[WhatsApp] Autenticado.');
});

client.on('ready', () => {
  whatsappState = 'ready';
  latestQrDataUrl = null;
  lastError = null;
  me = client.info ? {
    wid: client.info.wid?._serialized,
    pushname: client.info.pushname || null,
    platform: client.info.platform || null
  } : null;
  console.log('[WhatsApp] Cliente pronto.');
});

client.on('auth_failure', (message) => {
  whatsappState = 'auth_failure';
  lastError = String(message || 'Falha na autenticação');
});

client.on('disconnected', (reason) => {
  whatsappState = 'disconnected';
  lastError = String(reason || 'Desconectado');
  me = null;
});

app.get('/', (_req, res) => {
  res.type('html').send(`<!doctype html><html lang="pt-BR"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>WhatsApp Personal Bridge</title></head><body style="font-family:Arial,sans-serif;max-width:760px;margin:40px auto;padding:0 20px"><h1>WhatsApp Personal Bridge</h1><p>Serviço ativo.</p><p><a href="/qr">Abrir QR Code</a></p><p>Status: <strong>${whatsappState}</strong></p></body></html>`);
});

app.get('/health', (_req, res) => {
  res.json({
    ok: true,
    service: 'whatsapp-personal-render',
    whatsappState,
    ready: whatsappState === 'ready',
    hasQr: Boolean(latestQrDataUrl),
    lastError
  });
});

app.get('/qr', (req, res) => {
  // Se QR_SECRET estiver configurado, exige ?key=. Se não estiver, /qr abre normalmente.
  if (QR_SECRET && req.query.key !== QR_SECRET) {
    return res.status(401).type('html').send('<h1>401 - chave do QR inválida</h1><p>Use /qr?key=SUA_CHAVE.</p>');
  }

  let content;
  if (latestQrDataUrl) {
    content = `<div class="badge">Aguardando leitura</div><img src="${latestQrDataUrl}" alt="QR Code do WhatsApp" class="qr"><p>Abra o WhatsApp → Dispositivos conectados → Conectar dispositivo.</p>`;
  } else if (whatsappState === 'ready' || whatsappState === 'authenticated') {
    content = '<div class="ok">✓</div><h2>WhatsApp conectado</h2><p>A sessão foi autenticada com sucesso.</p>';
  } else {
    content = '<div class="spinner"></div><h2>Gerando QR Code...</h2><p>A página atualiza automaticamente.</p>';
  }

  res.type('html').send(`<!doctype html>
<html lang="pt-BR"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta http-equiv="refresh" content="4"><title>Conectar WhatsApp</title>
<style>*{box-sizing:border-box}body{margin:0;min-height:100vh;display:grid;place-items:center;background:#eef3f1;font-family:Arial,sans-serif;color:#172b24;padding:24px}.card{width:min(600px,100%);background:#fff;border-radius:24px;padding:32px;text-align:center;box-shadow:0 12px 40px rgba(0,0,0,.1)}.qr{display:block;width:min(520px,100%);height:auto;margin:22px auto;background:#fff;border:1px solid #dce5e1;border-radius:16px;padding:10px}.badge{display:inline-block;padding:8px 14px;background:#fff3cd;color:#725700;border-radius:999px;font-weight:700}.ok{width:84px;height:84px;border-radius:50%;display:grid;place-items:center;margin:20px auto;background:#dff7e8;color:#13763d;font-size:48px;font-weight:bold}.spinner{width:50px;height:50px;border:6px solid #d8e1dd;border-top-color:#25d366;border-radius:50%;margin:24px auto;animation:s 1s linear infinite}@keyframes s{to{transform:rotate(360deg)}}.state{margin-top:18px;color:#657970;font-size:14px}</style></head>
<body><main class="card"><h1>Conectar WhatsApp</h1>${content}<div class="state">Estado: <strong>${whatsappState}</strong>${lastError ? `<br>Erro: ${String(lastError).replace(/</g, '&lt;')}` : ''}</div></main></body></html>`);
});

app.get('/openapi.json', (req, res) => {
  const serverUrl = process.env.PUBLIC_BASE_URL || `${req.protocol}://${req.get('host')}`;
  res.json({
    openapi: '3.1.0',
    info: { title: 'Personal WhatsApp Bridge API', version: '1.0.1' },
    servers: [{ url: serverUrl }],
    components: { securitySchemes: { bearerAuth: { type: 'http', scheme: 'bearer' } } },
    security: [{ bearerAuth: [] }],
    paths: {
      '/api/status': { get: { operationId: 'getWhatsAppStatus', summary: 'Get WhatsApp status', responses: { '200': { description: 'Status' } } } },
      '/api/chats': { get: { operationId: 'listChats', summary: 'List chats', parameters: [{ name: 'limit', in: 'query', schema: { type: 'integer', default: 30, maximum: 100 } }], responses: { '200': { description: 'Chats' } } } },
      '/api/chats/{chatId}/messages': { get: { operationId: 'getChatMessages', summary: 'Get chat messages', parameters: [{ name: 'chatId', in: 'path', required: true, schema: { type: 'string' } }, { name: 'limit', in: 'query', schema: { type: 'integer', default: 30, maximum: 100 } }], responses: { '200': { description: 'Messages' } } } },
      '/api/search': { get: { operationId: 'searchMessages', summary: 'Search messages', parameters: [{ name: 'q', in: 'query', required: true, schema: { type: 'string' } }], responses: { '200': { description: 'Results' } } } },
      '/api/send': { post: { operationId: 'sendWhatsAppMessage', summary: 'Send one WhatsApp message', requestBody: { required: true, content: { 'application/json': { schema: { type: 'object', required: ['to', 'message'], properties: { to: { type: 'string' }, message: { type: 'string', maxLength: 5000 } } } } } }, responses: { '200': { description: 'Sent' } } } }
    }
  });
});

app.get('/api/status', requireApiToken, (_req, res) => res.json({ state: whatsappState, ready: whatsappState === 'ready', me, lastError }));

app.get('/api/chats', requireApiToken, async (req, res) => {
  try {
    if (whatsappState !== 'ready') return res.status(409).json({ error: 'WhatsApp is not ready', state: whatsappState });
    const limit = Math.min(Math.max(Number(req.query.limit || 30), 1), 100);
    const chats = await client.getChats();
    res.json({ chats: chats.slice(0, limit).map(serializeChat) });
  } catch (error) { res.status(500).json({ error: error.message }); }
});

app.get('/api/chats/:chatId/messages', requireApiToken, async (req, res) => {
  try {
    if (whatsappState !== 'ready') return res.status(409).json({ error: 'WhatsApp is not ready', state: whatsappState });
    const limit = Math.min(Math.max(Number(req.query.limit || 30), 1), 100);
    const chat = await client.getChatById(req.params.chatId);
    const messages = await chat.fetchMessages({ limit });
    res.json({ chat: serializeChat(chat), messages: messages.map(serializeMessage) });
  } catch (error) { res.status(500).json({ error: error.message }); }
});

app.get('/api/search', requireApiToken, async (req, res) => {
  try {
    if (whatsappState !== 'ready') return res.status(409).json({ error: 'WhatsApp is not ready', state: whatsappState });
    const q = String(req.query.q || '').trim().toLowerCase();
    if (!q) return res.status(400).json({ error: 'Query parameter q is required' });
    const chats = (await client.getChats()).slice(0, 30);
    const results = [];
    for (const chat of chats) {
      const messages = await chat.fetchMessages({ limit: 50 });
      for (const message of messages) {
        if ((message.body || '').toLowerCase().includes(q)) results.push({ chat: serializeChat(chat), message: serializeMessage(message) });
      }
    }
    results.sort((a, b) => (b.message.timestamp || 0) - (a.message.timestamp || 0));
    res.json({ query: q, results: results.slice(0, 100) });
  } catch (error) { res.status(500).json({ error: error.message }); }
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
    if (!(await client.isRegisteredUser(chatId))) return res.status(404).json({ error: 'Number is not registered on WhatsApp' });
    const sent = await client.sendMessage(chatId, message);
    res.json({ ok: true, id: sent.id?._serialized, to: number, timestamp: sent.timestamp });
  } catch (error) { res.status(500).json({ error: error.message }); }
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
