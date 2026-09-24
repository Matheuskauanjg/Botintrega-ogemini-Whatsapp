import http from 'node:http';
import { createAudioShare, getAudioShare } from './audio-share-store.js';

const MAX_JSON_BYTES = 20 * 1024 * 1024;

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
        forwardAudioRequest(Buffer.concat(chunks));
      });
      return;
    }

    streamToBridge();

    function forwardAudioRequest(body) {
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
        upstreamRes.on('end', () => {
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
            const share = createAudioShare({
              buffer: audio,
              mimetype: data.mimetype || 'audio/ogg; codecs=opus',
              filename: `whatsapp-${String(data.messageId || 'audio')}.ogg`
            });
            const base = publicBaseUrl();
            data.audioUrl = base ? `${base}/media/audio/${share.token}.ogg` : `/media/audio/${share.token}.ogg`;
            data.audioUrlExpiresAt = new Date(share.expiresAt).toISOString();
            data.audioUrlTtlSeconds = Math.max(0, Math.floor((share.expiresAt - Date.now()) / 1000));
            data.whisperHandoff = 'Use WhisperAI transcribe_url with audioUrl.';
            console.log(`[AudioShare] URL temporária criada para message=${String(data.messageId || '(unknown)')} bytes=${audio.length}`);
            sendJson(res, 200, data);
          } catch (error) {
            console.warn('[AudioShare] Falha ao preparar URL temporária:', error?.message || error);
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
    console.log(`[AudioShare] Internal handoff proxy listening on 127.0.0.1:${listenPort} -> bridge ${bridgePort}`);
  });

  return server;
}
