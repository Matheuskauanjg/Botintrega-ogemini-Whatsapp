import http from 'node:http';
import zlib from 'node:zlib';

const MAX_BODY_BYTES = 20 * 1024 * 1024;
const INTERNAL_CLIENT_ID = 'chatgpt-meu-whatsapp';

function tryJson(text) {
  try { return JSON.parse(text); } catch { return null; }
}

function parseMaybeNested(value) {
  if (value && typeof value === 'object') return value;
  if (typeof value !== 'string') return null;
  const trimmed = value.trim();
  if (!trimmed) return null;
  const direct = tryJson(trimmed);
  if (direct) return direct;
  if (/^[A-Za-z0-9+/=_-]+$/.test(trimmed) && trimmed.length >= 8) {
    try {
      const decoded = Buffer.from(trimmed, 'base64').toString('utf8').trim();
      return tryJson(decoded);
    } catch {
      return null;
    }
  }
  return null;
}

function decodeMcpBody(buffer, headers) {
  let raw = buffer;
  const contentEncoding = String(headers['content-encoding'] || '').toLowerCase();
  if (contentEncoding.includes('gzip') || (raw.length >= 2 && raw[0] === 0x1f && raw[1] === 0x8b)) {
    try { raw = zlib.gunzipSync(raw); } catch (_) {}
  }

  let text = raw.toString('utf8').replace(/^\uFEFF/, '').trim();
  let parsed = tryJson(text);
  if (!parsed && raw.length > 4) {
    const remaining = raw.length - 4;
    const be = raw.readUInt32BE(0);
    const le = raw.readUInt32LE(0);
    if (be === remaining || le === remaining) {
      text = raw.subarray(4).toString('utf8').replace(/^\uFEFF/, '').trim();
      parsed = tryJson(text);
    }
  }
  if (typeof parsed === 'string') parsed = parseMaybeNested(parsed) || parsed;
  if (parsed && typeof parsed === 'object' && !Array.isArray(parsed) && !parsed.method) {
    for (const key of ['request', 'payload', 'body', 'data']) {
      const nested = parseMaybeNested(parsed[key]);
      if (nested && typeof nested === 'object' && (nested.method || nested.jsonrpc)) {
        parsed = nested;
        break;
      }
    }
  }
  if (Array.isArray(parsed) && parsed.length === 1 && parsed[0] && typeof parsed[0] === 'object') parsed = parsed[0];
  return { parsed, decodedBytes: raw.length };
}

function logSafeShape(parsed, byteLength) {
  const kind = Array.isArray(parsed) ? 'array' : typeof parsed;
  const method = parsed && typeof parsed === 'object' && !Array.isArray(parsed) ? String(parsed.method || '(none)') : '(none)';
  const jsonrpc = parsed && typeof parsed === 'object' && !Array.isArray(parsed) ? String(parsed.jsonrpc || '(none)') : '(none)';
  const keys = parsed && typeof parsed === 'object' && !Array.isArray(parsed) ? Object.keys(parsed).slice(0, 12).join(',') : '';
  console.log(`[MCP-PROXY] decoded bytes=${byteLength} kind=${kind} method=${method} jsonrpc=${jsonrpc} keys=[${keys}]`);
}

function logMcpHeaders(headers) {
  const protocol = String(headers['mcp-protocol-version'] || '(none)');
  const method = String(headers['mcp-method'] || '(none)');
  const name = String(headers['mcp-name'] || '(none)');
  const session = headers['mcp-session-id'] ? 'present' : 'none';
  console.log(`[MCP-PROXY] mcp-headers protocol=${protocol} method=${method} name=${name} session=${session}`);
}

function publicBaseUrl(req) {
  const configured = String(process.env.PUBLIC_BASE_URL || '').replace(/\/$/, '');
  if (configured) return configured;
  const proto = String(req.headers['x-forwarded-proto'] || 'https').split(',')[0].trim();
  return `${proto}://${req.headers.host}`;
}

function isChatGptCimdClientId(value) {
  try {
    const parsed = new URL(String(value || ''));
    if (parsed.protocol !== 'https:' || parsed.hostname !== 'chatgpt.com') return false;
    if (parsed.pathname === '/oauth/client.json') return true;
    return /^\/oauth\/[^/]+\/client\.json$/.test(parsed.pathname);
  } catch {
    return false;
  }
}

function rewriteClientIdValue(value) {
  if (isChatGptCimdClientId(value)) {
    console.log('[OAuth-PROXY] ChatGPT CIMD client_id -> internal predefined client');
    return INTERNAL_CLIENT_ID;
  }
  return value;
}

function rewriteClientIdInPath(rawUrl) {
  try {
    const parsed = new URL(rawUrl, 'https://mcp.local');
    if (parsed.pathname !== '/oauth/authorize') return rawUrl;
    const current = parsed.searchParams.get('client_id');
    if (!current) return rawUrl;
    const rewritten = rewriteClientIdValue(current);
    if (rewritten === current) return rawUrl;
    parsed.searchParams.set('client_id', rewritten);
    return `${parsed.pathname}${parsed.search}`;
  } catch {
    return rawUrl;
  }
}

function rewriteFormClientId(buffer) {
  const text = buffer.toString('utf8');
  const params = new URLSearchParams(text);
  const current = params.get('client_id');
  if (!current) return buffer;
  const rewritten = rewriteClientIdValue(current);
  if (rewritten === current) return buffer;
  params.set('client_id', rewritten);
  return Buffer.from(params.toString(), 'utf8');
}

export function startPublicMcpProxy({ publicPort, targetPort }) {
  const server = http.createServer((req, res) => {
    const headers = { ...req.headers };
    const originalContentType = String(headers['content-type'] || '');
    const originalAccept = String(headers.accept || '');
    const isMcp = req.url?.startsWith('/mcp');

    if (req.method === 'GET' && req.url?.split('?')[0] === '/.well-known/oauth-authorization-server') {
      const base = publicBaseUrl(req);
      res.writeHead(200, { 'content-type': 'application/json; charset=utf-8', 'cache-control': 'no-store' });
      res.end(JSON.stringify({
        issuer: base,
        authorization_endpoint: `${base}/oauth/authorize`,
        token_endpoint: `${base}/oauth/token`,
        response_types_supported: ['code'],
        grant_types_supported: ['authorization_code'],
        code_challenge_methods_supported: ['S256'],
        token_endpoint_auth_methods_supported: ['none'],
        authorization_response_iss_parameter_supported: true,
        client_id_metadata_document_supported: true,
        scopes_supported: ['whatsapp.read', 'whatsapp.send']
      }));
      console.log('[OAuth-PROXY] authorization metadata -> CIMD enabled');
      return;
    }

    if (req.method === 'GET' && req.url?.startsWith('/oauth/authorize')) {
      streamRequest(rewriteClientIdInPath(req.url));
      return;
    }

    if (req.method === 'POST' && (req.url?.startsWith('/oauth/token') || req.url?.startsWith('/oauth/authorize'))) {
      const chunks = [];
      let size = 0;
      req.on('data', chunk => {
        size += chunk.length;
        if (size <= MAX_BODY_BYTES) chunks.push(chunk);
      });
      req.on('end', () => {
        if (size > MAX_BODY_BYTES) {
          res.writeHead(413, { 'content-type': 'application/json' });
          res.end(JSON.stringify({ error: 'OAuth request body too large' }));
          return;
        }
        const rewritten = rewriteFormClientId(Buffer.concat(chunks));
        forwardRequest(rewritten, false, req.url);
      });
      return;
    }

    if (req.method === 'POST' && isMcp) {
      console.log(`[MCP-PROXY] POST ${req.url} content-type=\"${originalContentType || '(none)'}\" accept=\"${originalAccept || '(none)'}\"`);
      logMcpHeaders(headers);
      const chunks = [];
      let size = 0;
      req.on('data', chunk => {
        size += chunk.length;
        if (size <= MAX_BODY_BYTES) chunks.push(chunk);
      });
      req.on('end', () => {
        if (size > MAX_BODY_BYTES) {
          res.writeHead(413, { 'content-type': 'application/json' });
          res.end(JSON.stringify({ error: 'MCP request body too large' }));
          return;
        }
        const originalBody = Buffer.concat(chunks);
        if (originalBody.length === 0 && !headers['mcp-method']) {
          console.log('[MCP-PROXY] empty transport probe -> HTTP 204');
          res.writeHead(204, { 'cache-control': 'no-store', 'allow': 'POST, GET, DELETE' });
          res.end();
          return;
        }
        const { parsed, decodedBytes } = decodeMcpBody(originalBody, req.headers);
        logSafeShape(parsed, decodedBytes);
        if (!parsed || typeof parsed !== 'object') {
          console.warn('[MCP-PROXY] non-empty MCP payload could not be decoded as JSON-RPC; forwarding unchanged for SDK classification');
          forwardRequest(originalBody, false);
          return;
        }
        forwardRequest(Buffer.from(JSON.stringify(parsed), 'utf8'), true);
      });
      req.on('error', error => {
        console.error('[MCP-PROXY] request read error:', error);
        if (!res.headersSent) {
          res.writeHead(400, { 'content-type': 'application/json' });
          res.end(JSON.stringify({ error: 'Could not read MCP request body' }));
        }
      });
      return;
    }

    if (req.method === 'GET' && isMcp && originalAccept.trim() === '*/*' && !headers['mcp-session-id']) {
      console.log('[MCP-PROXY] GET transport probe accept="*/*" -> HTTP 204');
      res.writeHead(204, { 'cache-control': 'no-store', 'allow': 'POST, GET, DELETE' });
      res.end();
      return;
    }

    streamRequest();

    function forwardRequest(bodyBuffer, normalizedJson, pathOverride = req.url) {
      const forwardHeaders = { ...headers };
      if (normalizedJson) {
        forwardHeaders['content-type'] = 'application/json';
        forwardHeaders.accept = 'application/json, text/event-stream';
        delete forwardHeaders['content-encoding'];
      }
      forwardHeaders['content-length'] = String(bodyBuffer.length);
      forwardHeaders.host = `127.0.0.1:${targetPort}`;
      delete forwardHeaders.connection;
      const upstream = http.request({ hostname: '127.0.0.1', port: targetPort, method: req.method, path: pathOverride, headers: forwardHeaders }, upstreamRes => {
        res.writeHead(upstreamRes.statusCode || 502, upstreamRes.headers);
        upstreamRes.pipe(res);
      });
      upstream.on('error', error => {
        console.error('[MCP-PROXY] upstream error:', error);
        if (!res.headersSent) {
          res.writeHead(502, { 'content-type': 'application/json' });
          res.end(JSON.stringify({ error: 'MCP gateway unavailable' }));
        } else res.end();
      });
      upstream.end(bodyBuffer);
    }

    function streamRequest(pathOverride = req.url) {
      const forwardHeaders = { ...headers, host: `127.0.0.1:${targetPort}` };
      delete forwardHeaders.connection;
      const upstream = http.request({ hostname: '127.0.0.1', port: targetPort, method: req.method, path: pathOverride, headers: forwardHeaders }, upstreamRes => {
        res.writeHead(upstreamRes.statusCode || 502, upstreamRes.headers);
        upstreamRes.pipe(res);
      });
      upstream.on('error', error => {
        console.error('[MCP-PROXY] upstream error:', error);
        if (!res.headersSent) {
          res.writeHead(502, { 'content-type': 'application/json' });
          res.end(JSON.stringify({ error: 'MCP gateway unavailable' }));
        } else res.end();
      });
      req.pipe(upstream);
    }
  });

  server.listen(publicPort, '0.0.0.0', () => {
    console.log(`[MCP-PROXY] Public compatibility proxy listening on 0.0.0.0:${publicPort}`);
    console.log(`[MCP-PROXY] Forwarding to http://127.0.0.1:${targetPort}`);
    console.log(`[MCP-PROXY] Max request body: ${Math.round(MAX_BODY_BYTES / 1024 / 1024)} MB`);
  });

  return server;
}
