import crypto from 'node:crypto';

const DEFAULT_TTL_MS = 5 * 60 * 1000;
const MAX_ENTRIES = 40;
const shares = new Map();

function cleanupExpired() {
  const now = Date.now();
  for (const [token, entry] of shares.entries()) {
    if (!entry || entry.expiresAt <= now || entry.remainingReads <= 0) shares.delete(token);
  }
  while (shares.size > MAX_ENTRIES) {
    const first = shares.keys().next().value;
    if (!first) break;
    shares.delete(first);
  }
}

export function createAudioShare({ buffer, mimetype = 'audio/ogg; codecs=opus', filename = 'whatsapp-audio.ogg', ttlMs = DEFAULT_TTL_MS } = {}) {
  if (!Buffer.isBuffer(buffer) || !buffer.length) throw new Error('Audio buffer is empty');
  cleanupExpired();
  const token = crypto.randomBytes(32).toString('base64url');
  const expiresAt = Date.now() + Math.max(30_000, Math.min(Number(ttlMs) || DEFAULT_TTL_MS, 10 * 60 * 1000));
  shares.set(token, {
    buffer,
    mimetype: String(mimetype || 'audio/ogg; codecs=opus'),
    filename: String(filename || 'whatsapp-audio.ogg').replace(/[^a-zA-Z0-9._-]/g, '_'),
    expiresAt,
    remainingReads: 3
  });
  return { token, expiresAt };
}

export function getAudioShare(token, { consume = false } = {}) {
  cleanupExpired();
  const entry = shares.get(String(token || ''));
  if (!entry || entry.expiresAt <= Date.now()) {
    shares.delete(String(token || ''));
    return null;
  }
  if (consume) {
    entry.remainingReads -= 1;
    if (entry.remainingReads <= 0) {
      setTimeout(() => shares.delete(String(token || '')), 10_000).unref?.();
    }
  }
  return entry;
}

setInterval(cleanupExpired, 60_000).unref?.();
