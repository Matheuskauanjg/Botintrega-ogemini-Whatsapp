import fs from 'node:fs';
import path from 'node:path';
import { DatabaseSync } from 'node:sqlite';

function safeJson(value) {
  try { return JSON.stringify(value); }
  catch { return null; }
}

function parseJson(value, fallback = null) {
  try { return value ? JSON.parse(value) : fallback; }
  catch { return fallback; }
}

export function createDiscordStore(dbPath) {
  const resolved = path.resolve(dbPath);
  fs.mkdirSync(path.dirname(resolved), { recursive: true });

  const db = new DatabaseSync(resolved, { timeout: 5000 });
  db.exec(`
    PRAGMA journal_mode=WAL;
    PRAGMA synchronous=NORMAL;

    CREATE TABLE IF NOT EXISTS discord_channels (
      channel_id TEXT PRIMARY KEY,
      guild_id TEXT,
      guild_name TEXT,
      name TEXT,
      type TEXT,
      last_ts INTEGER,
      updated_at INTEGER NOT NULL
    );

    CREATE TABLE IF NOT EXISTS discord_messages (
      channel_id TEXT NOT NULL,
      message_id TEXT NOT NULL,
      guild_id TEXT,
      author_id TEXT,
      author_name TEXT,
      content TEXT,
      ts INTEGER,
      attachments_json TEXT,
      referenced_message_id TEXT,
      raw_json TEXT,
      updated_at INTEGER NOT NULL,
      PRIMARY KEY (channel_id, message_id)
    );

    CREATE INDEX IF NOT EXISTS idx_discord_messages_channel_ts
      ON discord_messages(channel_id, ts DESC);
    CREATE INDEX IF NOT EXISTS idx_discord_messages_content
      ON discord_messages(content);
  `);

  const upsertChannelStmt = db.prepare(`
    INSERT INTO discord_channels(channel_id, guild_id, guild_name, name, type, last_ts, updated_at)
    VALUES (?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT(channel_id) DO UPDATE SET
      guild_id=COALESCE(excluded.guild_id, discord_channels.guild_id),
      guild_name=COALESCE(excluded.guild_name, discord_channels.guild_name),
      name=COALESCE(excluded.name, discord_channels.name),
      type=COALESCE(excluded.type, discord_channels.type),
      last_ts=CASE
        WHEN excluded.last_ts IS NULL THEN discord_channels.last_ts
        ELSE MAX(COALESCE(discord_channels.last_ts, 0), excluded.last_ts)
      END,
      updated_at=excluded.updated_at
  `);

  const upsertMessageStmt = db.prepare(`
    INSERT INTO discord_messages(
      channel_id, message_id, guild_id, author_id, author_name, content, ts,
      attachments_json, referenced_message_id, raw_json, updated_at
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT(channel_id, message_id) DO UPDATE SET
      guild_id=COALESCE(excluded.guild_id, discord_messages.guild_id),
      author_id=COALESCE(excluded.author_id, discord_messages.author_id),
      author_name=COALESCE(excluded.author_name, discord_messages.author_name),
      content=excluded.content,
      ts=COALESCE(excluded.ts, discord_messages.ts),
      attachments_json=COALESCE(excluded.attachments_json, discord_messages.attachments_json),
      referenced_message_id=COALESCE(excluded.referenced_message_id, discord_messages.referenced_message_id),
      raw_json=COALESCE(excluded.raw_json, discord_messages.raw_json),
      updated_at=excluded.updated_at
  `);

  function upsertChannel(channel) {
    const channelId = String(channel?.id || channel?.channelId || '').trim();
    if (!channelId) return;
    upsertChannelStmt.run(
      channelId,
      channel?.guildId ?? null,
      channel?.guildName ?? null,
      channel?.name ?? null,
      channel?.type == null ? null : String(channel.type),
      Number.isFinite(Number(channel?.timestamp)) ? Number(channel.timestamp) : null,
      Date.now()
    );
  }

  function upsertMessage(message) {
    const channelId = String(message?.channelId || '').trim();
    const messageId = String(message?.id || '').trim();
    if (!channelId || !messageId) return;
    const ts = Number.isFinite(Number(message?.timestamp)) ? Number(message.timestamp) : null;
    upsertMessageStmt.run(
      channelId,
      messageId,
      message?.guildId ?? null,
      message?.authorId ?? null,
      message?.authorName ?? null,
      message?.content ?? '',
      ts,
      safeJson(message?.attachments || []),
      message?.referencedMessageId ?? null,
      safeJson(message?.raw || null),
      Date.now()
    );
    upsertChannel({
      id: channelId,
      guildId: message?.guildId ?? null,
      guildName: message?.guildName ?? null,
      name: message?.channelName ?? null,
      type: message?.channelType ?? null,
      timestamp: ts
    });
  }

  function rowToMessage(row) {
    return {
      id: row.message_id,
      channelId: row.channel_id,
      guildId: row.guild_id || null,
      authorId: row.author_id || null,
      authorName: row.author_name || null,
      content: row.content || '',
      timestamp: row.ts == null ? null : Number(row.ts),
      attachments: parseJson(row.attachments_json, []),
      referencedMessageId: row.referenced_message_id || null
    };
  }

  function listChannels(limit = 100) {
    return db.prepare(`
      SELECT * FROM discord_channels
      ORDER BY COALESCE(last_ts, 0) DESC, updated_at DESC
      LIMIT ?
    `).all(Math.max(1, Math.min(Number(limit) || 100, 500))).map(row => ({
      id: row.channel_id,
      guildId: row.guild_id || null,
      guildName: row.guild_name || null,
      name: row.name || null,
      type: row.type || null,
      timestamp: row.last_ts == null ? null : Number(row.last_ts)
    }));
  }

  function listMessages(channelId, limit = 100) {
    const rows = db.prepare(`
      SELECT * FROM discord_messages
      WHERE channel_id=?
      ORDER BY COALESCE(ts, 0) DESC, updated_at DESC
      LIMIT ?
    `).all(String(channelId), Math.max(1, Math.min(Number(limit) || 100, 500)));
    return rows.reverse().map(rowToMessage);
  }

  function searchMessages(query, limit = 100) {
    const q = String(query || '').trim();
    if (!q) return [];
    return db.prepare(`
      SELECT * FROM discord_messages
      WHERE content LIKE ? ESCAPE '\\'
      ORDER BY COALESCE(ts, 0) DESC
      LIMIT ?
    `).all(`%${q.replace(/[\\%_]/g, value => `\\${value}`)}%`, Math.max(1, Math.min(Number(limit) || 100, 500)))
      .map(row => ({ channelId: row.channel_id, message: rowToMessage(row) }));
  }

  function getMessage(channelId, messageId) {
    const row = db.prepare(`
      SELECT * FROM discord_messages WHERE channel_id=? AND message_id=?
    `).get(String(channelId), String(messageId));
    return row ? rowToMessage(row) : null;
  }

  function stats() {
    const messages = db.prepare('SELECT COUNT(*) AS count FROM discord_messages').get()?.count || 0;
    const channels = db.prepare('SELECT COUNT(*) AS count FROM discord_channels').get()?.count || 0;
    return { path: resolved, messages: Number(messages), channels: Number(channels) };
  }

  function close() {
    try { db.close(); } catch {}
  }

  return {
    path: resolved,
    upsertChannel,
    upsertMessage,
    listChannels,
    listMessages,
    searchMessages,
    getMessage,
    stats,
    close
  };
}
