import fs from 'node:fs';
import path from 'node:path';
import express from 'express';
import {
  AttachmentBuilder,
  ChannelType,
  Client,
  Events,
  GatewayIntentBits,
  Partials,
  PermissionFlagsBits
} from 'discord.js';
import { createDiscordStore } from './discord-store.js';

const AUDIO_EXTENSIONS = new Set(['.ogg', '.oga', '.mp3', '.wav', '.m4a', '.aac', '.flac', '.webm', '.mp4']);
const IMAGE_EXTENSIONS = new Set(['.jpg', '.jpeg', '.png', '.gif', '.webp']);

function bool(value, fallback = false) {
  if (value == null || value === '') return fallback;
  return ['1', 'true', 'yes', 'on'].includes(String(value).toLowerCase());
}

function clamp(value, min, max, fallback) {
  const parsed = Number(value);
  if (!Number.isFinite(parsed)) return fallback;
  return Math.max(min, Math.min(max, Math.trunc(parsed)));
}

function extensionFromType(mimetype, fallback = '.bin') {
  const type = String(mimetype || '').split(';')[0].trim().toLowerCase();
  const map = {
    'image/jpeg': '.jpg',
    'image/png': '.png',
    'image/gif': '.gif',
    'image/webp': '.webp',
    'audio/ogg': '.ogg',
    'audio/mpeg': '.mp3',
    'audio/wav': '.wav',
    'audio/x-wav': '.wav',
    'audio/mp4': '.m4a',
    'audio/aac': '.aac',
    'audio/flac': '.flac',
    'audio/webm': '.webm'
  };
  return map[type] || fallback;
}

function attachmentArray(message) {
  return [...message.attachments.values()].map(item => ({
    id: item.id,
    name: item.name || null,
    url: item.url,
    proxyUrl: item.proxyURL || null,
    contentType: item.contentType || null,
    size: item.size || null,
    width: item.width || null,
    height: item.height || null,
    duration: item.duration || null
  }));
}

function serializeMessage(message) {
  return {
    id: message.id,
    channelId: message.channelId,
    channelName: message.channel?.name || null,
    channelType: message.channel?.type == null ? null : String(message.channel.type),
    guildId: message.guildId || null,
    guildName: message.guild?.name || null,
    authorId: message.author?.id || null,
    authorName: message.member?.displayName || message.author?.globalName || message.author?.username || null,
    authorUsername: message.author?.username || null,
    bot: Boolean(message.author?.bot),
    content: message.content || '',
    timestamp: message.createdTimestamp || null,
    editedTimestamp: message.editedTimestamp || null,
    attachments: attachmentArray(message),
    referencedMessageId: message.reference?.messageId || null,
    mentions: {
      users: [...message.mentions.users.keys()],
      roles: [...message.mentions.roles.keys()]
    }
  };
}

function channelLabel(channel) {
  if (channel.type === ChannelType.DM) return channel.recipient?.globalName || channel.recipient?.username || 'DM';
  return channel.name || channel.id;
}

function isUsableTextChannel(channel) {
  return Boolean(channel && channel.isTextBased?.() && channel.messages && typeof channel.send === 'function');
}

function channelPermissions(channel, clientUser) {
  if (!channel?.guild || !clientUser) {
    return { view: true, readHistory: true, send: true, attachFiles: true, addReactions: true };
  }
  const permissions = channel.permissionsFor(clientUser);
  return {
    view: Boolean(permissions?.has(PermissionFlagsBits.ViewChannel)),
    readHistory: Boolean(permissions?.has(PermissionFlagsBits.ReadMessageHistory)),
    send: Boolean(permissions?.has(PermissionFlagsBits.SendMessages)),
    attachFiles: Boolean(permissions?.has(PermissionFlagsBits.AttachFiles)),
    addReactions: Boolean(permissions?.has(PermissionFlagsBits.AddReactions))
  };
}

function chooseDbPath() {
  if (process.env.DISCORD_DB_PATH) return process.env.DISCORD_DB_PATH;
  if (fs.existsSync('/data')) return '/data/discord.sqlite';
  return path.resolve(process.cwd(), '.data/discord.sqlite');
}

async function bufferFromUrl(url, maxBytes) {
  const response = await fetch(url, { signal: AbortSignal.timeout(30000) });
  if (!response.ok) throw new Error(`Falha ao baixar mídia: HTTP ${response.status}`);
  const declared = Number(response.headers.get('content-length') || 0);
  if (declared && declared > maxBytes) throw new Error(`Mídia excede o limite de ${maxBytes} bytes.`);
  const buffer = Buffer.from(await response.arrayBuffer());
  if (buffer.length > maxBytes) throw new Error(`Mídia excede o limite de ${maxBytes} bytes.`);
  return { buffer, mimetype: response.headers.get('content-type') || null };
}

async function transcribeWithGroq(buffer, mimetype, filename) {
  const apiKey = process.env.GROQ_API_KEY || '';
  if (!apiKey) return null;
  const model = process.env.GROQ_TRANSCRIBE_MODEL || 'whisper-large-v3-turbo';
  const language = process.env.GROQ_TRANSCRIBE_LANGUAGE || 'pt';
  const form = new FormData();
  form.append('file', new Blob([buffer], { type: mimetype || 'audio/ogg' }), filename || 'audio.ogg');
  form.append('model', model);
  if (language) form.append('language', language);
  form.append('response_format', 'json');

  const response = await fetch('https://api.groq.com/openai/v1/audio/transcriptions', {
    method: 'POST',
    headers: { authorization: `Bearer ${apiKey}` },
    body: form,
    signal: AbortSignal.timeout(clamp(process.env.GROQ_TRANSCRIBE_TIMEOUT_MS, 5000, 120000, 45000))
  });
  const text = await response.text();
  let data;
  try { data = text ? JSON.parse(text) : {}; }
  catch { data = { raw: text }; }
  if (!response.ok) throw new Error(data?.error?.message || data?.error || `Groq retornou HTTP ${response.status}`);
  return data?.text || null;
}

export function startDiscordService({ listenPort }) {
  const API_TOKEN = process.env.API_TOKEN || '';
  const DISCORD_BOT_TOKEN = process.env.DISCORD_BOT_TOKEN || '';
  const maxMediaBytes = clamp(process.env.DISCORD_MAX_MEDIA_BYTES, 1024 * 1024, 50 * 1024 * 1024, 24 * 1024 * 1024);
  const syncHistory = bool(process.env.DISCORD_SYNC_HISTORY, true);
  const syncMessageLimit = clamp(process.env.DISCORD_HISTORY_SYNC_LIMIT, 0, 100, 20);
  const syncChannelLimit = clamp(process.env.DISCORD_HISTORY_SYNC_CHANNEL_LIMIT, 1, 500, 100);
  const store = createDiscordStore(chooseDbPath());

  const client = new Client({
    intents: [
      GatewayIntentBits.Guilds,
      GatewayIntentBits.GuildMessages,
      GatewayIntentBits.DirectMessages,
      GatewayIntentBits.MessageContent
    ],
    partials: [Partials.Channel, Partials.Message]
  });

  let lastLoginError = null;
  let readyAt = null;
  let historySync = { running: false, channelsScanned: 0, messagesStored: 0, lastFinishedAt: null, lastError: null };

  async function resolveChannel(channelId) {
    const id = String(channelId || '').trim();
    if (!id) throw new Error('channelId é obrigatório.');
    const channel = client.channels.cache.get(id) || await client.channels.fetch(id).catch(() => null);
    if (!channel) throw new Error('Canal do Discord não encontrado ou sem acesso para o bot.');
    if (!isUsableTextChannel(channel)) throw new Error('O canal não suporta mensagens de texto.');
    return channel;
  }

  async function resolveSendChannel({ channelId, userId }) {
    if (channelId) return resolveChannel(channelId);
    const id = String(userId || '').trim();
    if (!id) throw new Error('Informe channelId ou userId.');
    const user = await client.users.fetch(id);
    return user.createDM();
  }

  function rememberChannel(channel, timestamp = null) {
    if (!channel) return;
    store.upsertChannel({
      id: channel.id,
      guildId: channel.guildId || null,
      guildName: channel.guild?.name || null,
      name: channelLabel(channel),
      type: channel.type,
      timestamp
    });
  }

  function rememberMessage(message) {
    if (!message?.id || !message?.channelId) return null;
    const serialized = serializeMessage(message);
    store.upsertMessage(serialized);
    return serialized;
  }

  async function syncRecentHistory() {
    if (!syncHistory || syncMessageLimit <= 0 || historySync.running || !client.isReady()) return;
    historySync = { ...historySync, running: true, lastError: null, channelsScanned: 0, messagesStored: 0 };
    try {
      const channels = [];
      for (const guild of client.guilds.cache.values()) {
        const fetched = await guild.channels.fetch().catch(() => null);
        if (!fetched) continue;
        for (const channel of fetched.values()) {
          if (channels.length >= syncChannelLimit) break;
          if (!isUsableTextChannel(channel)) continue;
          const permissions = channelPermissions(channel, client.user);
          if (!permissions.view || !permissions.readHistory) continue;
          rememberChannel(channel);
          channels.push(channel);
        }
        if (channels.length >= syncChannelLimit) break;
      }

      for (const channel of channels) {
        historySync.channelsScanned += 1;
        try {
          const messages = await channel.messages.fetch({ limit: syncMessageLimit });
          for (const message of messages.values()) {
            rememberMessage(message);
            historySync.messagesStored += 1;
          }
        } catch (error) {
          console.warn(`[Discord] Falha ao sincronizar #${channelLabel(channel)}: ${error.message}`);
        }
      }
      historySync.lastFinishedAt = Date.now();
    } catch (error) {
      historySync.lastError = error.message;
      console.error('[Discord] Erro na sincronização inicial:', error);
    } finally {
      historySync.running = false;
    }
  }

  client.once(Events.ClientReady, readyClient => {
    readyAt = Date.now();
    lastLoginError = null;
    console.log(`[Discord] Conectado como ${readyClient.user.tag} em ${readyClient.guilds.cache.size} servidor(es).`);
    for (const channel of readyClient.channels.cache.values()) if (channel.isTextBased?.()) rememberChannel(channel);
    void syncRecentHistory();
  });

  client.on(Events.MessageCreate, message => {
    try { rememberMessage(message); }
    catch (error) { console.warn('[Discord] Falha ao persistir mensagem:', error.message); }
  });

  client.on(Events.MessageUpdate, async (_oldMessage, newMessage) => {
    try {
      const full = newMessage.partial ? await newMessage.fetch() : newMessage;
      rememberMessage(full);
    } catch (error) {
      console.warn('[Discord] Falha ao atualizar mensagem persistida:', error.message);
    }
  });

  client.on(Events.ChannelCreate, channel => {
    if (channel.isTextBased?.()) rememberChannel(channel);
  });
  client.on(Events.ChannelUpdate, (_oldChannel, newChannel) => {
    if (newChannel.isTextBased?.()) rememberChannel(newChannel);
  });
  client.on(Events.Error, error => console.error('[Discord] Client error:', error));
  client.on(Events.Warn, warning => console.warn('[Discord] Client warning:', warning));

  const app = express();
  app.use(express.json({ limit: '35mb' }));
  app.use((req, res, next) => {
    if (!API_TOKEN) return res.status(503).json({ error: 'API_TOKEN não configurado.' });
    const auth = String(req.headers.authorization || '');
    if (auth !== `Bearer ${API_TOKEN}`) return res.status(401).json({ error: 'Não autorizado.' });
    next();
  });

  app.get('/discord/status', (_req, res) => {
    res.json({
      configured: Boolean(DISCORD_BOT_TOKEN),
      ready: client.isReady(),
      user: client.user ? { id: client.user.id, username: client.user.username, tag: client.user.tag } : null,
      guildCount: client.guilds.cache.size,
      cachedChannelCount: client.channels.cache.size,
      readyAt,
      lastLoginError,
      historySync,
      storage: store.stats(),
      messageContentIntentRequested: true
    });
  });

  app.get('/discord/guilds', (_req, res) => {
    const guilds = [...client.guilds.cache.values()].map(guild => ({
      id: guild.id,
      name: guild.name,
      memberCount: guild.memberCount,
      ownerId: guild.ownerId,
      iconUrl: guild.iconURL() || null
    })).sort((a, b) => a.name.localeCompare(b.name));
    res.json({ guilds });
  });

  app.get('/discord/chats', async (req, res) => {
    const limit = clamp(req.query.limit, 1, 500, 100);
    try {
      const live = [];
      for (const guild of client.guilds.cache.values()) {
        const fetched = await guild.channels.fetch().catch(() => null);
        if (!fetched) continue;
        for (const channel of fetched.values()) {
          if (!channel?.isTextBased?.()) continue;
          const permissions = channelPermissions(channel, client.user);
          rememberChannel(channel);
          live.push({
            id: channel.id,
            name: channelLabel(channel),
            guildId: guild.id,
            guildName: guild.name,
            type: String(channel.type),
            permissions
          });
        }
      }
      for (const channel of client.channels.cache.values()) {
        if (channel.type !== ChannelType.DM) continue;
        rememberChannel(channel);
        live.push({
          id: channel.id,
          name: channelLabel(channel),
          guildId: null,
          guildName: null,
          type: String(channel.type),
          permissions: channelPermissions(channel, client.user)
        });
      }
      const deduped = [...new Map(live.map(item => [item.id, item])).values()];
      const persisted = store.listChannels(limit);
      res.json({ channels: deduped.slice(0, limit), persisted });
    } catch (error) {
      res.status(500).json({ error: error.message });
    }
  });

  app.get('/discord/channels/:channelId/messages', async (req, res) => {
    const limit = clamp(req.query.limit, 1, 100, 30);
    try {
      const channel = await resolveChannel(req.params.channelId);
      const permissions = channelPermissions(channel, client.user);
      if (!permissions.readHistory) return res.status(403).json({ error: 'Bot sem permissão Read Message History neste canal.' });
      const messages = await channel.messages.fetch({ limit });
      const serialized = [...messages.values()]
        .sort((a, b) => a.createdTimestamp - b.createdTimestamp)
        .map(message => rememberMessage(message));
      res.json({ channelId: channel.id, messages: serialized });
    } catch (error) {
      res.status(400).json({ error: error.message });
    }
  });

  app.get('/discord/search', (req, res) => {
    const query = String(req.query.q || '').trim();
    const limit = clamp(req.query.limit, 1, 500, 100);
    if (!query) return res.status(400).json({ error: 'q é obrigatório.' });
    res.json({ query, results: store.searchMessages(query, limit) });
  });

  app.get('/discord/db-stats', (_req, res) => {
    res.json(store.stats());
  });

  app.post('/discord/send', async (req, res) => {
    try {
      const {
        channelId,
        userId,
        message,
        replyToMessageId,
        mentionUserIds = [],
        mentionRoleIds = [],
        prependMentions = true
      } = req.body || {};
      if (!String(message || '').trim()) return res.status(400).json({ error: 'message é obrigatório.' });
      const channel = await resolveSendChannel({ channelId, userId });
      const permissions = channelPermissions(channel, client.user);
      if (!permissions.send) return res.status(403).json({ error: 'Bot sem permissão Send Messages neste canal.' });

      const userMentions = [...new Set((Array.isArray(mentionUserIds) ? mentionUserIds : []).map(String).filter(Boolean))].slice(0, 50);
      const roleMentions = [...new Set((Array.isArray(mentionRoleIds) ? mentionRoleIds : []).map(String).filter(Boolean))].slice(0, 20);
      const mentionPrefix = prependMentions
        ? [...userMentions.map(id => `<@${id}>`), ...roleMentions.map(id => `<@&${id}>`)].join(' ')
        : '';
      const content = [mentionPrefix, String(message)].filter(Boolean).join(' ').slice(0, 2000);
      const sent = await channel.send({
        content,
        ...(replyToMessageId ? { reply: { messageReference: String(replyToMessageId), failIfNotExists: false } } : {}),
        allowedMentions: { users: userMentions, roles: roleMentions, repliedUser: false }
      });
      res.json({ ok: true, message: rememberMessage(sent) });
    } catch (error) {
      res.status(400).json({ error: error.message });
    }
  });

  app.post('/discord/react', async (req, res) => {
    try {
      const { channelId, messageId, emoji = '' } = req.body || {};
      const channel = await resolveChannel(channelId);
      const message = await channel.messages.fetch(String(messageId || ''));
      if (!message) return res.status(404).json({ error: 'Mensagem não encontrada.' });
      if (emoji) {
        await message.react(String(emoji));
        return res.json({ ok: true, action: 'added', emoji: String(emoji) });
      }
      let removed = 0;
      for (const reaction of message.reactions.cache.values()) {
        try {
          await reaction.users.remove(client.user.id);
          removed += 1;
        } catch {}
      }
      res.json({ ok: true, action: 'removed-own-reactions', removed });
    } catch (error) {
      res.status(400).json({ error: error.message });
    }
  });

  app.post('/discord/send-image', async (req, res) => {
    try {
      const {
        channelId,
        userId,
        imageUrl,
        imageBase64,
        mimetype,
        filename,
        caption = '',
        replyToMessageId,
        mentionUserIds = [],
        mentionRoleIds = [],
        prependMentions = true
      } = req.body || {};
      if (!imageUrl && !imageBase64) return res.status(400).json({ error: 'imageUrl ou imageBase64 é obrigatório.' });
      const channel = await resolveSendChannel({ channelId, userId });
      const permissions = channelPermissions(channel, client.user);
      if (!permissions.send || !permissions.attachFiles) return res.status(403).json({ error: 'Bot sem permissão para enviar anexos neste canal.' });

      let buffer;
      let detectedType = mimetype || null;
      if (imageUrl) {
        const downloaded = await bufferFromUrl(String(imageUrl), maxMediaBytes);
        buffer = downloaded.buffer;
        detectedType = detectedType || downloaded.mimetype;
      } else {
        const cleaned = String(imageBase64).replace(/^data:[^;]+;base64,/, '');
        buffer = Buffer.from(cleaned, 'base64');
        if (!buffer.length) throw new Error('imageBase64 inválido.');
        if (buffer.length > maxMediaBytes) throw new Error(`Imagem excede o limite de ${maxMediaBytes} bytes.`);
      }
      const ext = extensionFromType(detectedType, '.jpg');
      if (!IMAGE_EXTENSIONS.has(ext)) throw new Error(`Tipo de imagem não suportado: ${detectedType || ext}`);
      const safeFilename = String(filename || `discord-image${ext}`).replace(/[^a-zA-Z0-9._-]/g, '_');

      const userMentions = [...new Set((Array.isArray(mentionUserIds) ? mentionUserIds : []).map(String).filter(Boolean))].slice(0, 50);
      const roleMentions = [...new Set((Array.isArray(mentionRoleIds) ? mentionRoleIds : []).map(String).filter(Boolean))].slice(0, 20);
      const prefix = prependMentions
        ? [...userMentions.map(id => `<@${id}>`), ...roleMentions.map(id => `<@&${id}>`)].join(' ')
        : '';
      const content = [prefix, String(caption || '')].filter(Boolean).join(' ').slice(0, 2000);
      const sent = await channel.send({
        content: content || undefined,
        files: [new AttachmentBuilder(buffer, { name: safeFilename })],
        ...(replyToMessageId ? { reply: { messageReference: String(replyToMessageId), failIfNotExists: false } } : {}),
        allowedMentions: { users: userMentions, roles: roleMentions, repliedUser: false }
      });
      res.json({ ok: true, message: rememberMessage(sent) });
    } catch (error) {
      res.status(400).json({ error: error.message });
    }
  });

  app.post('/discord/audio', async (req, res) => {
    try {
      const { channelId, messageId } = req.body || {};
      const channel = await resolveChannel(channelId);
      const message = await channel.messages.fetch(String(messageId || ''));
      const serialized = rememberMessage(message);
      const audio = serialized.attachments.find(item => {
        const type = String(item.contentType || '').toLowerCase();
        const ext = path.extname(item.name || '').toLowerCase();
        return type.startsWith('audio/') || AUDIO_EXTENSIONS.has(ext);
      });
      if (!audio) return res.status(404).json({ error: 'Nenhum anexo de áudio encontrado nessa mensagem.' });
      const downloaded = await bufferFromUrl(audio.url, maxMediaBytes);
      const mimetype = audio.contentType || downloaded.mimetype || 'audio/ogg';
      const filename = audio.name || `discord-audio${extensionFromType(mimetype, '.ogg')}`;
      let transcript = null;
      let transcriptionError = null;
      try { transcript = await transcribeWithGroq(downloaded.buffer, mimetype, filename); }
      catch (error) { transcriptionError = error.message; }
      res.json({
        channelId: channel.id,
        messageId: message.id,
        authorId: message.author?.id || null,
        authorName: serialized.authorName,
        audioBase64: downloaded.buffer.toString('base64'),
        mimetype,
        filename,
        size: downloaded.buffer.length,
        transcript,
        transcriptionError
      });
    } catch (error) {
      res.status(400).json({ error: error.message });
    }
  });

  const server = app.listen(listenPort, '127.0.0.1', () => {
    console.log(`[Discord] Internal service listening on 127.0.0.1:${listenPort}`);
  });

  if (!DISCORD_BOT_TOKEN) {
    lastLoginError = 'DISCORD_BOT_TOKEN não configurado.';
    console.warn('[Discord] DISCORD_BOT_TOKEN ausente; integração Discord ficará desativada até configurar o token.');
  } else {
    client.login(DISCORD_BOT_TOKEN).catch(error => {
      lastLoginError = error.message;
      console.error('[Discord] Falha no login:', error);
    });
  }

  const cleanup = () => {
    try { server.close(); } catch {}
    try { client.destroy(); } catch {}
    try { store.close(); } catch {}
  };
  process.once('SIGTERM', cleanup);
  process.once('SIGINT', cleanup);

  return { app, server, client, store };
}
