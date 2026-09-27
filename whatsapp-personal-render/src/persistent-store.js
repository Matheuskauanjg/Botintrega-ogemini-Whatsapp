import fs from 'node:fs';
import path from 'node:path';
import { DatabaseSync } from 'node:sqlite';

function safeJson(value) {
  try {
    return JSON.stringify(value, (_key, item) => typeof item === 'bigint' ? item.toString() : item);
  } catch {
    return null;
  }
}

function parseJson(value, fallback = null) {
  try { return value ? JSON.parse(value) : fallback; }
  catch { return fallback; }
}

function boolInt(value) {
  return value ? 1 : 0;
}

function normalizeMapping(lid, pn) {
  const a = String(lid || '').trim();
  const b = String(pn || '').trim();
  if (a.endsWith('@lid') && b.endsWith('@s.whatsapp.net')) return { lid: a, pn: b };
  if (b.endsWith('@lid') && a.endsWith('@s.whatsapp.net')) return { lid: b, pn: a };
  return null;
}

export function createPersistentStore(dbPath) {
  const resolved = path.resolve(dbPath);
  fs.mkdirSync(path.dirname(resolved), { recursive: true });

  const db = new DatabaseSync(resolved, { timeout: 5000 });
  db.exec(`
    PRAGMA journal_mode=WAL;
    PRAGMA synchronous=NORMAL;
    PRAGMA foreign_keys=ON;

    CREATE TABLE IF NOT EXISTS chats (
      chat_id TEXT PRIMARY KEY,
      name TEXT,
      unread_count INTEGER NOT NULL DEFAULT 0,
      ts INTEGER,
      archived INTEGER NOT NULL DEFAULT 0,
      pinned INTEGER NOT NULL DEFAULT 0,
      updated_at INTEGER NOT NULL
    );

    CREATE TABLE IF NOT EXISTS contacts (
      jid TEXT PRIMARY KEY,
      name TEXT,
      notify TEXT,
      verified_name TEXT,
      raw_json TEXT,
      updated_at INTEGER NOT NULL
    );

    CREATE TABLE IF NOT EXISTS messages (
      chat_id TEXT NOT NULL,
      message_id TEXT NOT NULL,
      participant TEXT,
      from_me INTEGER NOT NULL DEFAULT 0,
      text TEXT,
      ts INTEGER,
      type TEXT,
      push_name TEXT,
      audio_json TEXT,
      raw_json TEXT,
      updated_at INTEGER NOT NULL,
      PRIMARY KEY (chat_id, message_id)
    );

    CREATE INDEX IF NOT EXISTS idx_messages_chat_ts ON messages(chat_id, ts DESC);
    CREATE INDEX IF NOT EXISTS idx_messages_text ON messages(text);

    CREATE TABLE IF NOT EXISTS jid_mapping (
      lid TEXT PRIMARY KEY,
      pn TEXT NOT NULL,
      updated_at INTEGER NOT NULL
    );

    CREATE UNIQUE INDEX IF NOT EXISTS idx_jid_mapping_pn ON jid_mapping(pn);
  `);

  const upsertChatStmt = db.prepare(`
    INSERT INTO chats(chat_id, name, unread_count, ts, archived, pinned, updated_at)
    VALUES (?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT(chat_id) DO UPDATE SET
      name=COALESCE(excluded.name, chats.name),
      unread_count=excluded.unread_count,
      ts=CASE WHEN excluded.ts IS NULL THEN chats.ts ELSE MAX(COALESCE(chats.ts, 0), excluded.ts) END,
      archived=excluded.archived,
      pinned=excluded.pinned,
      updated_at=excluded.updated_at
  `);

  const upsertContactStmt = db.prepare(`
    INSERT INTO contacts(jid, name, notify, verified_name, raw_json, updated_at)
    VALUES (?, ?, ?, ?, ?, ?)
    ON CONFLICT(jid) DO UPDATE SET
      name=COALESCE(excluded.name, contacts.name),
      notify=COALESCE(excluded.notify, contacts.notify),
      verified_name=COALESCE(excluded.verified_name, contacts.verified_name),
      raw_json=COALESCE(excluded.raw_json, contacts.raw_json),
      updated_at=excluded.updated_at
  `);

  const upsertMessageStmt = db.prepare(`
    INSERT INTO messages(chat_id, message_id, participant, from_me, text, ts, type, push_name, audio_json, raw_json, updated_at)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT(chat_id, message_id) DO UPDATE SET
      participant=COALESCE(excluded.participant, messages.participant),
      from_me=excluded.from_me,
      text=CASE WHEN excluded.text IS NULL OR excluded.text='' THEN messages.text ELSE excluded.text END,
      ts=COALESCE(excluded.ts, messages.ts),
      type=COALESCE(excluded.type, messages.type),
      push_name=COALESCE(excluded.push_name, messages.push_name),
      audio_json=COALESCE(excluded.audio_json, messages.audio_json),
      raw_json=COALESCE(excluded.raw_json, messages.raw_json),
      updated_at=excluded.updated_at
  `);

  const upsertMappingStmt = db.prepare(`
    INSERT INTO jid_mapping(lid, pn, updated_at)
    VALUES (?, ?, ?)
    ON CONFLICT(lid) DO UPDATE SET pn=excluded.pn, updated_at=excluded.updated_at
  `);

  function upsertChat(chat) {
    const id = String(chat?.id || chat?.chatId || '').trim();
    if (!id) return;
    const now = Date.now();
    upsertChatStmt.run(
      id,
      chat?.name ?? null,
      Number(chat?.unreadCount || 0),
      Number.isFinite(Number(chat?.timestamp)) ? Number(chat.timestamp) : null,
      boolInt(chat?.archived),
      boolInt(chat?.pinned),
      now
    );
  }

  function upsertContact(contact) {
    const id = String(contact?.id || '').trim();
    if (!id) return;
    upsertContactStmt.run(
      id,
      contact?.name ?? null,
      contact?.notify ?? null,
      contact?.verifiedName ?? null,
      safeJson(contact),
      Date.now()
    );
  }

  function upsertMessage(serialized, rawMessage = null) {
    const chatId = String(serialized?.chatId || '').trim();
    const messageId = String(serialized?.id || '').trim();
    if (!chatId || !messageId) return;
    const rawForStorage = rawMessage ? {
      key: rawMessage.key || null,
      message: rawMessage.message || null,
      pushName: rawMessage.pushName || null,
      messageTimestamp: serialized.timestamp ?? null
    } : null;
    upsertMessageStmt.run(
      chatId,
      messageId,
      serialized?.participant ?? null,
      boolInt(serialized?.fromMe),
      serialized?.text ?? '',
      Number.isFinite(Number(serialized?.timestamp)) ? Number(serialized.timestamp) : null,
      serialized?.type ?? null,
      serialized?.pushName ?? null,
      safeJson(serialized?.audio ?? null),
      safeJson(rawForStorage),
      Date.now()
    );
    upsertChat({ id: chatId, timestamp: serialized?.timestamp ?? null });
  }

  function upsertLidMapping(lid, pn) {
    const mapping = normalizeMapping(lid, pn);
    if (!mapping) return false;
    upsertMappingStmt.run(mapping.lid, mapping.pn, Date.now());
    return true;
  }

  function inferAndStoreMappings(key) {
    if (!key || typeof key !== 'object') return;
    const pairs = [
      [key.remoteJid, key.remoteJidAlt],
      [key.participant, key.participantAlt]
    ];
    for (const [a, b] of pairs) upsertLidMapping(a, b);
  }

  function rowToMessage(row) {
    return {
      id: row.message_id,
      chatId: row.chat_id,
      participant: row.participant || null,
      fromMe: Boolean(row.from_me),
      text: row.text || '',
      timestamp: row.ts == null ? null : Number(row.ts),
      type: row.type || null,
      pushName: row.push_name || null,
      audio: parseJson(row.audio_json, null)
    };
  }

  function listChats(limit = 100) {
    return db.prepare(`
      SELECT chat_id, name, unread_count, ts, archived, pinned
      FROM chats
      ORDER BY COALESCE(ts, 0) DESC
      LIMIT ?
    `).all(Math.max(1, Math.min(Number(limit) || 100, 500))).map(row => ({
      id: row.chat_id,
      name: row.name || null,
      unreadCount: Number(row.unread_count || 0),
      timestamp: row.ts == null ? null : Number(row.ts),
      archived: Boolean(row.archived),
      pinned: Boolean(row.pinned)
    }));
  }

  function listMessages(chatId, limit = 100) {
    const rows = db.prepare(`
      SELECT * FROM messages
      WHERE chat_id=?
      ORDER BY COALESCE(ts, 0) DESC, updated_at DESC
      LIMIT ?
    `).all(String(chatId), Math.max(1, Math.min(Number(limit) || 100, 500)));
    return rows.reverse().map(rowToMessage);
  }

  function searchMessages(query, limit = 100) {
    const q = String(query || '').trim();
    if (!q) return [];
    return db.prepare(`
      SELECT * FROM messages
      WHERE text LIKE ? ESCAPE '\\'
      ORDER BY COALESCE(ts, 0) DESC
      LIMIT ?
    `).all(`%${q.replace(/[\\%_]/g, value => `\\${value}`)}%`, Math.max(1, Math.min(Number(limit) || 100, 500)))
      .map(row => ({ chatId: row.chat_id, message: rowToMessage(row) }));
  }

  function getMessage(chatId, messageId) {
    const row = db.prepare(`SELECT * FROM messages WHERE chat_id=? AND message_id=?`).get(String(chatId), String(messageId));
    return row ? rowToMessage(row) : null;
  }

  function getQuotedMessage(chatId, messageId) {
    const row = db.prepare(`SELECT * FROM messages WHERE chat_id=? AND message_id=?`).get(String(chatId), String(messageId));
    if (!row) return null;
    const raw = parseJson(row.raw_json, null);
    if (raw?.key && raw?.message) return raw;
    const key = {
      remoteJid: row.chat_id,
      id: row.message_id,
      fromMe: Boolean(row.from_me),
      ...(row.participant ? { participant: row.participant } : {})
    };
    return {
      key,
      pushName: row.push_name || undefined,
      messageTimestamp: row.ts || undefined,
      message: { conversation: row.text || '' }
    };
  }

  function getMessageAuthor(chatId, messageId) {
    const message = getMessage(chatId, messageId);
    if (!message) return null;
    return message.participant || message.chatId || null;
  }

  function resolvePreferredJid(jid) {
    const value = String(jid || '').trim();
    if (!value) return value;
    if (value.endsWith('@lid')) {
      const row = db.prepare(`SELECT pn FROM jid_mapping WHERE lid=?`).get(value);
      if (row?.pn) return row.pn;
    }
    return value;
  }

  function stats() {
    const messages = db.prepare('SELECT COUNT(*) AS count FROM messages').get()?.count || 0;
    const chats = db.prepare('SELECT COUNT(*) AS count FROM chats').get()?.count || 0;
    const mappings = db.prepare('SELECT COUNT(*) AS count FROM jid_mapping').get()?.count || 0;
    return { path: resolved, messages: Number(messages), chats: Number(chats), jidMappings: Number(mappings) };
  }

  function close() {
    try { db.close(); } catch {}
  }

  return {
    path: resolved,
    upsertChat,
    upsertContact,
    upsertMessage,
    upsertLidMapping,
    inferAndStoreMappings,
    listChats,
    listMessages,
    searchMessages,
    getMessage,
    getQuotedMessage,
    getMessageAuthor,
    resolvePreferredJid,
    stats,
    close
  };
}
