import fs from 'node:fs/promises';
import path from 'node:path';

const GROQ_CHAT_URL = 'https://api.groq.com/openai/v1/chat/completions';
const DEFAULT_REPLY_MODEL = 'openai/gpt-oss-20b';
const POLL_MS = 2500;

function normalizeNumber(value) {
  let digits = String(value || '').replace(/\D/g, '');
  if (digits.length === 11) digits = `55${digits}`;
  return digits;
}

function normalizeControlJid(value) {
  const raw = String(value || '').trim();
  if (!raw) return '';

  if (raw.includes('@')) {
    const match = raw.match(/^([0-9]+)@(s\.whatsapp\.net|lid)$/i);
    if (!match) return '';
    return `${match[1]}@${match[2].toLowerCase()}`;
  }

  const number = normalizeNumber(raw);
  return number ? `${number}@s.whatsapp.net` : '';
}

function isDirectChatJid(value) {
  const jid = String(value || '').toLowerCase();
  return jid.endsWith('@s.whatsapp.net') || jid.endsWith('@lid');
}

function normalizeCommand(value) {
  return String(value || '').trim().toLowerCase().normalize('NFD').replace(/[\u0300-\u036f]/g, '').replace(/\s+/g, ' ');
}

function parseCommand(value) {
  const command = normalizeCommand(value);
  if (['on', 'auto on', 'automatico on'].includes(command)) return 'on';
  if (['off', 'auto off', 'automatico off'].includes(command)) return 'off';
  if (['status', 'auto status', 'automatico status'].includes(command)) return 'status';
  return null;
}

function sleep(ms) {
  return new Promise(resolve => setTimeout(resolve, ms));
}

export function startAutoReplyService({ bridgePort, audioPort }) {
  const API_TOKEN = String(process.env.API_TOKEN || '').trim();
  const GROQ_API_KEY = String(process.env.GROQ_API_KEY || '').trim();
  const CONTROL_INPUT = String(process.env.AUTO_REPLY_CONTROL_JID || process.env.AUTO_REPLY_CONTROL_NUMBER || '').trim();
  const CONTROL_JID = normalizeControlJid(CONTROL_INPUT);
  const REPLY_MODEL = String(process.env.GROQ_REPLY_MODEL || DEFAULT_REPLY_MODEL).trim() || DEFAULT_REPLY_MODEL;
  const PREFIX = String(process.env.AUTO_REPLY_PREFIX ?? '🤖 ').slice(0, 30);
  const statePath = process.env.AUTO_REPLY_STATE_PATH || (String(process.env.BAILEYS_AUTH_PATH || '').startsWith('/var/data/')
    ? '/var/data/whatsapp-auto-reply.json'
    : path.resolve(process.cwd(), '.whatsapp-auto-reply.json'));

  let state = { enabled: false, enabledAt: 0, lastControlMessageId: null };
  let timer = null;
  let stopped = false;
  const processed = new Set();
  const busy = new Set();
  const lastChatTimestamp = new Map();
  const startedAt = Math.floor(Date.now() / 1000);

  async function api(base, pathname, options = {}) {
    const headers = new Headers(options.headers || {});
    headers.set('authorization', `Bearer ${API_TOKEN}`);
    if (options.body) headers.set('content-type', 'application/json');
    const response = await fetch(`${base}${pathname}`, { ...options, headers });
    const raw = await response.text();
    let data;
    try { data = raw ? JSON.parse(raw) : {}; } catch { data = {}; }
    if (!response.ok) throw new Error(data?.error || `HTTP ${response.status}`);
    return data;
  }

  const bridge = (pathname, options) => api(`http://127.0.0.1:${bridgePort}`, pathname, options);
  const audio = (pathname, options) => api(`http://127.0.0.1:${audioPort}`, pathname, options);

  async function loadState() {
    try {
      const saved = JSON.parse(await fs.readFile(statePath, 'utf8'));
      state = {
        enabled: Boolean(saved?.enabled),
        enabledAt: Number(saved?.enabledAt || 0),
        lastControlMessageId: saved?.lastControlMessageId || null
      };
    } catch {}
  }

  async function saveState() {
    try {
      await fs.mkdir(path.dirname(statePath), { recursive: true });
      await fs.writeFile(statePath, JSON.stringify({ ...state, updatedAt: new Date().toISOString() }, null, 2), 'utf8');
    } catch (error) {
      console.warn('[AutoReply] state save failed:', error?.message || error);
    }
  }

  async function send(to, message) {
    return bridge('/api/send', { method: 'POST', body: JSON.stringify({ to, message }) });
  }

  function isControlChat(jid) {
    return Boolean(CONTROL_JID) && String(jid || '').toLowerCase() === CONTROL_JID.toLowerCase();
  }

  async function checkControl() {
    if (!CONTROL_JID) return;
    const data = await bridge(`/api/chats/${encodeURIComponent(CONTROL_JID)}/messages?limit=12`);
    const messages = Array.isArray(data?.messages) ? data.messages : [];
    for (const message of messages) {
      if (!message?.fromMe || !message?.id || message.id === state.lastControlMessageId) continue;
      const command = parseCommand(message.text);
      if (!command) continue;
      state.lastControlMessageId = message.id;
      if (command === 'on') {
        state.enabled = true;
        state.enabledAt = Math.floor(Date.now() / 1000);
        processed.clear();
        lastChatTimestamp.clear();
        await saveState();
        await send(CONTROL_JID, '🤖 Respostas automáticas: ON');
        console.log('[AutoReply] ON');
      } else if (command === 'off') {
        state.enabled = false;
        await saveState();
        await send(CONTROL_JID, '🔕 Respostas automáticas: OFF');
        console.log('[AutoReply] OFF');
      } else {
        await saveState();
        await send(CONTROL_JID, state.enabled ? '🤖 Respostas automáticas estão ON' : '🔕 Respostas automáticas estão OFF');
      }
    }
  }

  async function transcribe(chatId, messageId) {
    const data = await audio('/api/audio', { method: 'POST', body: JSON.stringify({ chatId, messageId }) });
    return data?.transcriptionStatus === 'ok' ? String(data?.transcript || '').trim() : '';
  }

  async function createReply(messages, currentText) {
    if (!GROQ_API_KEY) throw new Error('GROQ_API_KEY is not configured');
    const history = messages.slice(-12).flatMap(message => {
      const text = String(message?.text || '').trim();
      if (!text) return [];
      return [{ role: message.fromMe ? 'assistant' : 'user', content: text.slice(0, 1200) }];
    });
    if (!history.length || history.at(-1)?.role !== 'user') history.push({ role: 'user', content: currentText });

    const response = await fetch(GROQ_CHAT_URL, {
      method: 'POST',
      headers: { authorization: `Bearer ${GROQ_API_KEY}`, 'content-type': 'application/json' },
      body: JSON.stringify({
        model: REPLY_MODEL,
        messages: [
          {
            role: 'system',
            content: 'Escreva uma resposta automática curta e natural para WhatsApp em português do Brasil. Use apenas o contexto fornecido. Não invente fatos pessoais. Não confirme pagamentos, senhas, códigos, dados bancários, contratos ou compromissos importantes; nesses casos diga apenas que Matheus vai verificar e responder depois. Retorne somente o texto da resposta.'
          },
          ...history
        ],
        temperature: 0.7,
        max_completion_tokens: 220
      })
    });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload?.error?.message || `Groq HTTP ${response.status}`);
    return String(payload?.choices?.[0]?.message?.content || '').trim().slice(0, 1800);
  }

  async function processChat(chat) {
    const chatId = String(chat?.id || '');
    if (!isDirectChatJid(chatId) || isControlChat(chatId) || busy.has(chatId)) return;

    const cutoff = Math.max(startedAt, Number(state.enabledAt || 0));
    const chatTimestamp = Number(chat?.timestamp || 0);
    if (chatTimestamp && chatTimestamp < cutoff) {
      lastChatTimestamp.set(chatId, chatTimestamp);
      return;
    }
    if (chatTimestamp && lastChatTimestamp.get(chatId) === chatTimestamp) return;

    const data = await bridge(`/api/chats/${encodeURIComponent(chatId)}/messages?limit=16`);
    const messages = Array.isArray(data?.messages) ? data.messages : [];
    if (chatTimestamp) lastChatTimestamp.set(chatId, chatTimestamp);

    const incoming = messages.filter(message => message?.id && !message.fromMe && Number(message.timestamp || 0) >= cutoff && !processed.has(message.id));
    if (!incoming.length) return;

    for (const message of incoming) processed.add(message.id);
    if (processed.size > 4000) processed.clear();
    const current = incoming.at(-1);
    busy.add(chatId);
    try {
      let text = String(current?.text || '').trim();
      if (!text && current?.audio?.available) text = await transcribe(chatId, current.id);
      if (!text) return;
      const reply = await createReply(messages, text);
      if (!reply || !state.enabled) return;
      await sleep(2500 + Math.floor(Math.random() * 3000));
      if (!state.enabled) return;
      await send(chatId, `${PREFIX}${reply}`);
      console.log(`[AutoReply] sent chat=${chatId} message=${current.id}`);
    } catch (error) {
      console.warn(`[AutoReply] chat=${chatId} failed:`, error?.message || error);
    } finally {
      busy.delete(chatId);
    }
  }

  async function poll() {
    if (stopped) return;
    try {
      await checkControl();
      if (state.enabled) {
        const data = await bridge('/api/chats?limit=100');
        for (const chat of data?.chats || []) {
          if (!state.enabled) break;
          await processChat(chat);
        }
      }
    } catch (error) {
      console.warn('[AutoReply] poll failed:', error?.message || error);
    } finally {
      if (!stopped) timer = setTimeout(() => void poll(), POLL_MS);
    }
  }

  void (async () => {
    await loadState();
    if (!CONTROL_JID) console.warn('[AutoReply] Configure AUTO_REPLY_CONTROL_JID (preferred) or AUTO_REPLY_CONTROL_NUMBER; automatic mode cannot be controlled until then.');
    console.log(`[AutoReply] state=${state.enabled ? 'ON' : 'OFF'} model=${REPLY_MODEL} control=${CONTROL_JID ? 'configured' : 'missing'}`);
    timer = setTimeout(() => void poll(), 1500);
  })();

  return {
    stop() { stopped = true; clearTimeout(timer); },
    getState() { return { ...state, controlIdConfigured: Boolean(CONTROL_JID), model: REPLY_MODEL }; }
  };
}
