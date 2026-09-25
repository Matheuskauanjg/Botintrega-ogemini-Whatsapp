import http from 'node:http';
import { createAudioShare, getAudioShare } from './audio-share-store.js';

const MAX_JSON_BYTES = 20 * 1024 * 1024;
const GROQ_TRANSCRIBE_URL = 'https://api.groq.com/openai/v1/audio/transcriptions';
const DEFAULT_GROQ_MODEL = 'whisper-large-v3-turbo';

function publicBaseUrl() {
  return String(process.env.PUBLIC_BASE_URL || '').replace(/\/$/, '');
}

function sendJson(res, status, payload) {
  const body = Buffer.from(JSON.stringify(payload), 'utf8');
  res.writeHead(status, {
    'content-type': 'application/json; charset=utf-8',
    'content-length': String(body.length),
    'cache-control': 'no-store'
  });
  res.end(body);
}

function audioTokenFromPath(urlText) {
  try {
    const url = new URL(urlText, 'http://localhost');
    const match = url.pathname.match(/^\/media\/audio\/([A-Za-z0-9_-]{20,})\.ogg$/);
    return match?.[1] || null;
  } catch {
    return null;
  }
}

function groqAudioMime(value) {
  const raw = String(value || '').toLowerCase();
  if (raw.includes('webm')) return { mime: 'audio/webm', ext: 'webm' };
  if (raw.includes('mpeg') || raw.includes('mp3')) return { mime: 'audio/mpeg', ext: 'mp3' };
  if (raw.includes('wav')) return { mime: 'audio/wav', ext: 'wav' };
  if (raw.includes('mp4') || raw.includes('m4a')) return { mime: 'audio/mp4', ext: 'm4a' };
  if (raw.includes('flac')) return { mime: 'audio/flac', ext: 'flac' };
  return { mime: 'audio/ogg', ext: 'ogg' };
}

async function transcribeWithGroq(audio, mimetype, messageId) {
  const apiKey = String(process.env.GROQ_API_KEY || '').trim();
  const model = String(process.env.GROQ_TRANSCRIBE_MODEL || DEFAULT_GROQ_MODEL).trim() || DEFAULT_GROQ_MODEL;
  const language = String(process.env.GROQ_TRANSCRIBE_LANGUAGE || '').trim();

  if (!apiKey) {
    return {
      ok: false,
      provider: 'groq',
      model,
      reason: 'not_configured',
      error: 'GROQ_API_KEY is not configured.'
    };
  }

  const { mime, ext } = groqAudioMime(mimetype);
  const safeMessageId = String(messageId || 'audio').replace(/[^a-zA-Z0-9_-]/g, '').slice(0, 80) || 'audio';
  const form = new FormData();
  form.set('model', model);
  form.set('response_format', 'json');
  form.set('temperature', '0');
  if (language) form.set('language', language);
  form.set('file', new Blob([audio], { type: mime }), `whatsapp-${safeMessageId}.${ext}`);

  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 30000);

  try {
    const response = await fetch(GROQ_TRANSCRIBE_URL, {
      method: 'POST',
      headers: { authorization: `Bearer ${apiKey}` },
      body: form,
      signal: controller.signal
    });

    const raw = await response.text();
    let payload;
    try { payload = raw ? JSON.parse(raw) : {}; }
    catch { payload = { raw }; }

    if (!response.ok) {
      const detail = payload?.error?.message || payload?.message || `HTTP ${response.status}`;
      return {
        ok: false,
        provider: 'groq',
        model,
        reason: response.status === 429 ? 'rate_limited' : 'api_error',
        statusCode: response.status,
        error: String(detail).slice(0, 500)
      };
    }

    const transcript = String(payload?.text || '').trim();
    if (!transcript) {
      return {
        ok: false,
        provider: 'groq',
        model,
        reason: 'empty_transcript',
        error: 'Groq returned an empty transcript.'
      };
    }

    return {
      ok: true,
      provider: 'groq',
      model,
      transcript
    };
  } catch (error) {
    return {
      ok: false,
      provider: 'groq',
      model,
      reason: error?.name === 'AbortError' ? 'timeout' : 'network_error',
      error: error?.name === 'AbortError' ? 'Groq transcription timed out.' : String(error?.message || error).slice(0, 500)
    };
  } finally {
    clearTimeout(timeout);
  }
}

function addWhisperFallback(data, audio) {
  const share = createAudioShare({
    buffer: audio,
    mimetype: data.mimetype || 'audio/ogg; codecs=opus',
    filename: `whatsapp-${String(data.messageId || 'audio')}.ogg`
  });
  const base = publicBaseUrl();
  data.audioUrl = base ? `${base}/media/audio/${share.token}.ogg` : `/media/audio/${share.token}.ogg`;
  data.audioUrlExpiresAt = new Date(share.expiresAt).toISOString();
  data.audioUrlTtlSeconds = Math.max(0, Math.floor((share.expiresAt - Date.now()) / 1000));
  data.fallbackProvider = 'whisperai';
  data.whisperHandoff = 'Groq transcription was unavailable. Use the connected WhisperAI transcribe_url tool with audioUrl.';
  console.log(`[AudioShare] WhisperAI fallback URL criada para message=${String(data.messageId || '(unknown)')} bytes=${audio.length}`);
}

export function startAudioHandoffProxy({ listenPort, bridgePort }) {
  const server = http.createServer((req, res) => {
    const token = audioTokenFromPath(req.url || '');

    if (token && (req.method === 'GET' || req.method === 'HEAD')) {
      const entry = getAudioShare(token, { consume: req.method === 'GET' });
      if (!entry) {
        res.writeHead(404, { 'cache-control': 'no-store' });
        res.end();
        return;
      }

      const headers = {
        'content-type': entry.mimetype || 'audio/ogg',
        'content-length': String(entry.buffer.length),
        'content-disposition': `inline; filename=\"${entry.filename}\"`,
        'cache-control': 'no-store, private, max-age=0',
        'x-content-type-options': 'nosniff'
      };
      res.writeHead(200, headers);
      if (req.method === 'HEAD') res.end();
      else res.end(entry.buffer);
      return;
    }

    if (req.method === 'POST' && req.url?.split('?')[0] === '/api/audio') {
      const chunks = [];
      let size = 0;
      req.on('data', chunk => {
        size += chunk.length;
        if (size <= MAX_JSON_BYTES) chunks.push(chunk);
      });
      req.on('end', () => {
        if (size > MAX_JSON_BYTES) {
          sendJson(res, 413, { error: 'Request too large' });
          return;
        }
        void forwardAudioRequest(Buffer.concat(chunks));
      });
      return;
    }

    streamToBridge();

    async function forwardAudioRequest(body) {
      const headers = { ...req.headers, host: `127.0.0.1:${bridgePort}`, 'content-length': String(body.length) };
      delete headers.connection;
      const upstream = http.request({
        hostname: '127.0.0.1',
        port: bridgePort,
        method: 'POST',
        path: req.url,
        headers
      }, upstreamRes => {
        const responseChunks = [];
        let responseSize = 0;
        upstreamRes.on('data', chunk => {
          responseSize += chunk.length;
          if (responseSize <= MAX_JSON_BYTES) responseChunks.push(chunk);
        });
        upstreamRes.on('end', async () => {
          if (responseSize > MAX_JSON_BYTES) {
            sendJson(res, 502, { error: 'Audio response too large' });
            return;
          }
          const raw = Buffer.concat(responseChunks);
          if ((upstreamRes.statusCode || 500) < 200 || (upstreamRes.statusCode || 500) >= 300) {
            res.writeHead(upstreamRes.statusCode || 500, upstreamRes.headers);
            res.end(raw);
            return;
          }

          try {
            const data = JSON.parse(raw.toString('utf8'));
            const audio = Buffer.from(String(data.audioBase64 || ''), 'base64');
            if (!audio.length) throw new Error('Audio payload is empty');

            const transcription = await transcribeWithGroq(audio, data.mimetype, data.messageId);
            data.transcriptionProvider = 'groq';
            data.transcriptionModel = transcription.model;

            if (transcription.ok) {
              data.transcriptionStatus = 'ok';
              data.transcript = transcription.transcript;
              data.fallbackProvider = 'whisperai';
              console.log(`[Audio] Groq transcription ok message=${String(data.messageId || '(unknown)')} chars=${transcription.transcript.length}`);
            } else {
              data.transcriptionStatus = 'fallback_required';
              data.transcript = null;
              data.transcriptionReason = transcription.reason;
              data.transcriptionError = transcription.error;
              if (transcription.statusCode) data.transcriptionHttpStatus = transcription.statusCode;
              addWhisperFallback(data, audio);
              console.warn(`[Audio] Groq indisponível (${transcription.reason}); fallback WhisperAI preparado para message=${String(data.messageId || '(unknown)')}`);
            }

            sendJson(res, 200, data);
          } catch (error) {
            console.warn('[Audio] Falha ao processar transcrição/fallback:', error?.message || error);
            res.writeHead(upstreamRes.statusCode || 200, upstreamRes.headers);
            res.end(raw);
          }
        });
      });
      upstream.on('error', error => sendJson(res, 502, { error: error.message }));
      upstream.end(body);
    }

    function streamToBridge() {
      const headers = { ...req.headers, host: `127.0.0.1:${bridgePort}` };
      delete headers.connection;
      const upstream = http.request({
        hostname: '127.0.0.1',
        port: bridgePort,
        method: req.method,
        path: req.url,
        headers
      }, upstreamRes => {
        res.writeHead(upstreamRes.statusCode || 502, upstreamRes.headers);
        upstreamRes.pipe(res);
      });
      upstream.on('error', error => {
        if (!res.headersSent) sendJson(res, 502, { error: error.message });
        else res.end();
      });
      req.pipe(upstream);
    }
  });

  server.listen(listenPort, '127.0.0.1', () => {
    console.log(`[Audio] Internal transcription proxy listening on 127.0.0.1:${listenPort} -> bridge ${bridgePort}`);
    console.log(`[Audio] Primary: Groq ${String(process.env.GROQ_TRANSCRIBE_MODEL || DEFAULT_GROQ_MODEL)} | Fallback: WhisperAI temporary URL`);
  });

  return server;
}
