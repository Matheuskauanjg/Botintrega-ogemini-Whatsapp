import crypto from 'node:crypto';
import { spawn } from 'node:child_process';
import { AsyncLocalStorage } from 'node:async_hooks';
import express from 'express';
import { createMcpHandler, McpServer } from '@modelcontextprotocol/server';
import { toNodeHandler } from '@modelcontextprotocol/node';
import * as z from 'zod/v4';

const PUBLIC_PORT = Number(process.env.PORT || 10000);
const BRIDGE_PORT = Number(process.env.BRIDGE_INTERNAL_PORT || 10001);
const BRIDGE_APP_MODULE = process.env.BRIDGE_APP_MODULE || 'bridge_chain';
const API_TOKEN = process.env.API_TOKEN || '';
const LOGIN_SECRET = process.env.MCP_LOGIN_SECRET || API_TOKEN || '';
const CLIENT_ID = process.env.MCP_CLIENT_ID || 'chatgpt-meu-discord';
const PUBLIC_BASE_URL = String(process.env.PUBLIC_BASE_URL || '').replace(/\/$/, '');
const BRIDGE_BASE = `http://127.0.0.1:${BRIDGE_PORT}`;
const STABLE_CHATGPT_REDIRECT = 'https://chatgpt.com/connector_platform_oauth_redirect';
const OAUTH_SCOPES = ['discord.read', 'discord.send'];
const ACCESS_TOKEN_TTL_SECONDS = 60 * 60 * 24 * 30;
const AUTH_CODE_TTL_MS = 5 * 60 * 1000;

const oauthCodes = new Map();
const requestContext = new AsyncLocalStorage();

let python = null;
let pythonRestartCount = 0;
let pythonRestartTimer = null;
let shuttingDown = false;

function startPythonBridge() {
  python = spawn(process.env.PYTHON_BIN || 'python3', [
    '-m', 'uvicorn', `${BRIDGE_APP_MODULE}:app`, '--host', '127.0.0.1', '--port', String(BRIDGE_PORT)
  ], {
    cwd: process.cwd(),
    env: process.env,
    stdio: 'inherit'
  });
  const startedAt = Date.now();
  python.on('exit', (code, signal) => {
    console.error(`[DiscordBridge] Python process exited code=${code} signal=${signal || ''}`);
    if (shuttingDown) return;
    pythonRestartCount += 1;
    const livedMs = Date.now() - startedAt;
    const delay = livedMs > 60_000 ? 1_000 : Math.min(30_000, 1_000 * 2 ** Math.min(pythonRestartCount, 5));
    console.error(`[DiscordBridge] restarting Python bridge in ${delay}ms (restart #${pythonRestartCount})`);
    pythonRestartTimer = setTimeout(startPythonBridge, delay);
  });
}

startPythonBridge();

function base64url(value) {
  return Buffer.from(value).toString('base64url');
}

function escapeHtml(value) {
  return String(value ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/\"/g, '&quot;')
    .replace(/'/g, '&#039;');
}

function safeEqual(a, b) {
  const aa = Buffer.from(String(a ?? ''));
  const bb = Buffer.from(String(b ?? ''));
  return aa.length === bb.length && crypto.timingSafeEqual(aa, bb);
}

function randomToken(bytes = 32) {
  return crypto.randomBytes(bytes).toString('base64url');
}

function hmac(input, secret) {
  return crypto.createHmac('sha256', secret).update(input).digest('base64url');
}

function signAccessToken(payload, secret) {
  const body = base64url(JSON.stringify(payload));
  return `${body}.${hmac(body, secret)}`;
}

function verifyAccessToken(token, secret, expectedIssuer, expectedResource) {
  if (!token || !secret || !token.includes('.')) return null;
  const [body, signature] = token.split('.');
  if (!body || !signature || !safeEqual(signature, hmac(body, secret))) return null;
  try {
    const payload = JSON.parse(Buffer.from(body, 'base64url').toString('utf8'));
    if (!payload.exp || payload.exp <= Math.floor(Date.now() / 1000)) return null;
    if (payload.iss !== expectedIssuer || payload.aud !== expectedResource) return null;
    return payload;
  } catch {
    return null;
  }
}

function requestBaseUrl(req) {
  if (PUBLIC_BASE_URL) return PUBLIC_BASE_URL;
  const proto = req.get('x-forwarded-proto') || req.protocol || 'https';
  return `${proto}://${req.get('host')}`;
}

function allowedRedirect(redirectUri) {
  try {
    const parsed = new URL(String(redirectUri || ''));
    if (parsed.protocol !== 'https:' || parsed.hostname !== 'chatgpt.com') return false;
    if (parsed.toString() === STABLE_CHATGPT_REDIRECT) return true;
    return parsed.pathname.startsWith('/connector/oauth/');
  } catch {
    return false;
  }
}

function requestedScopes(scopeText) {
  return String(scopeText || OAUTH_SCOPES.join(' '))
    .split(/\s+/)
    .filter(Boolean)
    .filter(scope => OAUTH_SCOPES.includes(scope));
}

function hasScopes(payload, requiredScopes) {
  if (!payload) return false;
  const granted = new Set(String(payload.scope || '').split(/\s+/).filter(Boolean));
  return requiredScopes.every(scope => granted.has(scope));
}

function textResult(value) {
  return { content: [{ type: 'text', text: typeof value === 'string' ? value : JSON.stringify(value, null, 2) }] };
}

function audioResult(value) {
  const { audioBase64, mimetype, ...meta } = value || {};
  if (!audioBase64) return errorResult('Audio payload is empty');
  return {
    content: [
      { type: 'audio', data: audioBase64, mimeType: mimetype || 'audio/ogg' },
      { type: 'text', text: JSON.stringify(meta, null, 2) }
    ]
  };
}

function errorResult(message) {
  return { content: [{ type: 'text', text: String(message) }], isError: true };
}

async function bridgeJson(pathname, options = {}) {
  if (!API_TOKEN) throw new Error('API_TOKEN is not configured.');
  const headers = new Headers(options.headers || {});
  headers.set('authorization', `Bearer ${API_TOKEN}`);
  if (options.body && !headers.has('content-type')) headers.set('content-type', 'application/json');
  const startedAt = performance.now();
  const response = await fetch(`${BRIDGE_BASE}${pathname}`, { ...options, headers });
  const text = await response.text();
  let data;
  try { data = text ? JSON.parse(text) : {}; } catch { data = { raw: text }; }
  if (!response.ok) {
    const detail = data?.detail || data?.error || `Discord bridge returned HTTP ${response.status}`;
    throw new Error(typeof detail === 'string' ? detail : JSON.stringify(detail));
  }
  if (data && typeof data === 'object' && !Array.isArray(data)) {
    data.internalGatewayLatencyMs = Math.round(performance.now() - startedAt);
  }
  return data;
}

function authDescriptor(scopes) {
  const schemes = [{ type: 'oauth2', scopes }];
  return { securitySchemes: schemes, _meta: { securitySchemes: schemes } };
}

function authFailure(requiredScopes) {
  const ctx = requestContext.getStore() || {};
  const metadataUrl = ctx.metadataUrl || `${PUBLIC_BASE_URL}/.well-known/oauth-protected-resource`;
  const challenge = `Bearer resource_metadata=\"${metadataUrl}\", scope=\"${requiredScopes.join(' ')}\", error=\"insufficient_scope\", error_description=\"Connect your Discord bridge to continue\"`;
  return {
    content: [{ type: 'text', text: 'Authentication required: connect your Discord bridge to continue.' }],
    _meta: { 'mcp/www_authenticate': [challenge] },
    isError: true
  };
}

function requireToolAuth(requiredScopes) {
  const ctx = requestContext.getStore() || {};
  if (!LOGIN_SECRET || !hasScopes(ctx.payload, requiredScopes)) return authFailure(requiredScopes);
  return null;
}

function createDiscordMcpServer() {
  const server = new McpServer({ name: 'meu-discord', version: '1.0.0' });

  server.registerTool('discord_status', {
    title: 'Status do Discord',
    description: 'Verifica se a conta pessoal do Discord está conectada e mostra dados básicos da sessão.',
    inputSchema: z.object({}),
    ...authDescriptor(['discord.read']),
    annotations: { readOnlyHint: true, destructiveHint: false, openWorldHint: false }
  }, async () => {
    const denied = requireToolAuth(['discord.read']);
    if (denied) return denied;
    try { return textResult(await bridgeJson('/api/status')); }
    catch (error) { return errorResult(error.message); }
  });

  server.registerTool('list_discord_chats', {
    title: 'Listar conversas do Discord',
    description: 'Lista DMs, grupos e canais de servidores visíveis pela conta conectada.',
    inputSchema: z.object({
      limit: z.number().int().min(1).max(100).default(30),
      includeGuilds: z.boolean().default(true),
      includeDMs: z.boolean().default(true)
    }),
    ...authDescriptor(['discord.read']),
    annotations: { readOnlyHint: true, destructiveHint: false, openWorldHint: false }
  }, async ({ limit, includeGuilds, includeDMs }) => {
    const denied = requireToolAuth(['discord.read']);
    if (denied) return denied;
    try {
      return textResult(await bridgeJson(`/api/chats?limit=${encodeURIComponent(limit)}&includeGuilds=${includeGuilds}&includeDMs=${includeDMs}`));
    } catch (error) { return errorResult(error.message); }
  });

  server.registerTool('read_discord_messages', {
    title: 'Ler mensagens do Discord',
    description: 'Lê as mensagens recentes de um canal, DM ou grupo usando o channelId.',
    inputSchema: z.object({
      channelId: z.string().min(1),
      limit: z.number().int().min(1).max(100).default(30)
    }),
    ...authDescriptor(['discord.read']),
    annotations: { readOnlyHint: true, destructiveHint: false, openWorldHint: false }
  }, async ({ channelId, limit }) => {
    const denied = requireToolAuth(['discord.read']);
    if (denied) return denied;
    try { return textResult(await bridgeJson(`/api/chats/${encodeURIComponent(channelId)}/messages?limit=${encodeURIComponent(limit)}`)); }
    catch (error) { return errorResult(error.message); }
  });

  server.registerTool('read_discord_audio', {
    title: 'Ouvir áudio do Discord',
    description: 'Baixa um áudio ou mensagem de voz anexada e, se houver GROQ_API_KEY, também transcreve.',
    inputSchema: z.object({
      channelId: z.string().min(1),
      messageId: z.string().min(1)
    }),
    ...authDescriptor(['discord.read']),
    annotations: { readOnlyHint: true, destructiveHint: false, openWorldHint: true }
  }, async ({ channelId, messageId }) => {
    const denied = requireToolAuth(['discord.read']);
    if (denied) return denied;
    try {
      return audioResult(await bridgeJson('/api/audio', {
        method: 'POST', body: JSON.stringify({ channelId, messageId })
      }));
    } catch (error) { return errorResult(error.message); }
  });

  server.registerTool('search_discord_messages', {
    title: 'Pesquisar mensagens do Discord',
    description: 'Pesquisa texto nas mensagens recentes das conversas visíveis. A busca é limitada para respeitar rate limits.',
    inputSchema: z.object({
      query: z.string().min(1).max(500),
      limit: z.number().int().min(1).max(100).default(30),
      maxChannels: z.number().int().min(1).max(40).default(15),
      perChannel: z.number().int().min(1).max(100).default(50)
    }),
    ...authDescriptor(['discord.read']),
    annotations: { readOnlyHint: true, destructiveHint: false, openWorldHint: true }
  }, async ({ query, limit, maxChannels, perChannel }) => {
    const denied = requireToolAuth(['discord.read']);
    if (denied) return denied;
    try {
      return textResult(await bridgeJson(`/api/search?q=${encodeURIComponent(query)}&limit=${limit}&maxChannels=${maxChannels}&perChannel=${perChannel}`));
    } catch (error) { return errorResult(error.message); }
  });

  server.registerTool('discord_cache_stats', {
    title: 'Estatísticas do cache do Discord',
    description: 'Mostra quantos servidores, conversas privadas, usuários e mensagens estão em cache na sessão.',
    inputSchema: z.object({}),
    ...authDescriptor(['discord.read']),
    annotations: { readOnlyHint: true, destructiveHint: false, openWorldHint: false }
  }, async () => {
    const denied = requireToolAuth(['discord.read']);
    if (denied) return denied;
    try { return textResult(await bridgeJson('/api/cache-stats')); }
    catch (error) { return errorResult(error.message); }
  });

  server.registerTool('send_discord_message', {
    title: 'Enviar ou responder mensagem no Discord',
    description: 'Envia texto para um channelId. Pode responder uma mensagem e mencionar usuários ou o autor da mensagem citada.',
    inputSchema: z.object({
      to: z.string().min(1).describe('ID numérico do canal, DM ou grupo.'),
      message: z.string().min(1).max(5000),
      replyToMessageId: z.string().min(1).optional(),
      mentionUserIds: z.array(z.string().min(1)).max(20).optional(),
      mentionAuthorOfMessageId: z.string().min(1).optional(),
      prependMentions: z.boolean().default(true)
    }),
    ...authDescriptor(['discord.send']),
    annotations: { readOnlyHint: false, destructiveHint: false, openWorldHint: false }
  }, async ({ to, message, replyToMessageId, mentionUserIds, mentionAuthorOfMessageId, prependMentions }) => {
    const denied = requireToolAuth(['discord.send']);
    if (denied) return denied;
    try {
      return textResult(await bridgeJson('/api/send', {
        method: 'POST',
        body: JSON.stringify({ to, message, replyToMessageId, mentionUserIds, mentionAuthorOfMessageId, prependMentions })
      }));
    } catch (error) { return errorResult(error.message); }
  });

  server.registerTool('react_discord_message', {
    title: 'Reagir a uma mensagem do Discord',
    description: 'Adiciona uma reação. Se emoji estiver vazio, tenta remover as reações da própria conta nessa mensagem.',
    inputSchema: z.object({
      to: z.string().min(1),
      messageId: z.string().min(1),
      emoji: z.string().max(100).default('')
    }),
    ...authDescriptor(['discord.send']),
    annotations: { readOnlyHint: false, destructiveHint: false, openWorldHint: false }
  }, async ({ to, messageId, emoji }) => {
    const denied = requireToolAuth(['discord.send']);
    if (denied) return denied;
    try {
      return textResult(await bridgeJson('/api/react', {
        method: 'POST', body: JSON.stringify({ to, messageId, emoji })
      }));
    } catch (error) { return errorResult(error.message); }
  });

  server.registerTool('send_discord_image', {
    title: 'Enviar imagem no Discord',
    description: 'Envia imagem por URL HTTPS ou base64 e pode responder uma mensagem específica.',
    inputSchema: z.object({
      to: z.string().min(1),
      imageUrl: z.string().url().optional(),
      imageBase64: z.string().optional(),
      mimetype: z.string().optional(),
      filename: z.string().max(200).optional(),
      caption: z.string().max(5000).optional(),
      replyToMessageId: z.string().min(1).optional(),
      mentionUserIds: z.array(z.string().min(1)).max(20).optional(),
      prependMentions: z.boolean().default(true)
    }),
    ...authDescriptor(['discord.send']),
    annotations: { readOnlyHint: false, destructiveHint: false, openWorldHint: true }
  }, async ({ to, imageUrl, imageBase64, mimetype, filename, caption, replyToMessageId, mentionUserIds, prependMentions }) => {
    const denied = requireToolAuth(['discord.send']);
    if (denied) return denied;
    if (!imageUrl && !imageBase64) return errorResult('imageUrl or imageBase64 is required');
    try {
      return textResult(await bridgeJson('/api/send-image', {
        method: 'POST',
        body: JSON.stringify({ to, imageUrl, imageBase64, mimetype, filename, caption, replyToMessageId, mentionUserIds, prependMentions })
      }));
    } catch (error) { return errorResult(error.message); }
  });

  return server;
}

const mcpHandler = createMcpHandler(createDiscordMcpServer);
const mcpNodeHandler = toNodeHandler(mcpHandler, {
  onerror(error) { console.error('[MCP] Adapter error:', error); }
});

const app = express();
app.set('trust proxy', true);

app.get('/health', (_req, res) => {
  res.json({
    ok: true,
    service: 'meu-discord-mcp',
    pythonRunning: Boolean(python && python.exitCode === null && !python.killed),
    pythonRestarts: pythonRestartCount
  });
});

app.get('/ready', async (_req, res) => {
  try {
    const response = await fetch(`${BRIDGE_BASE}/health`, { signal: AbortSignal.timeout(2500) });
    const payload = await response.json();
    const ready = Boolean(response.ok && payload?.discordReady && payload?.tokenConfigured);
    res.status(ready ? 200 : 503).json({
      ok: ready,
      pythonRunning: Boolean(python && python.exitCode === null && !python.killed),
      pythonRestarts: pythonRestartCount,
      bridge: payload
    });
  } catch (error) {
    res.status(503).json({
      ok: false,
      pythonRunning: Boolean(python && python.exitCode === null && !python.killed),
      pythonRestarts: pythonRestartCount,
      error: error.message
    });
  }
});

app.get('/.well-known/oauth-protected-resource', (req, res) => {
  const base = requestBaseUrl(req);
  res.json({
    resource: `${base}/mcp`,
    authorization_servers: [base],
    scopes_supported: OAUTH_SCOPES,
    resource_documentation: `${base}/mcp-info`
  });
});

app.get('/.well-known/oauth-authorization-server', (req, res) => {
  const base = requestBaseUrl(req);
  res.json({
    issuer: base,
    authorization_endpoint: `${base}/oauth/authorize`,
    token_endpoint: `${base}/oauth/token`,
    response_types_supported: ['code'],
    grant_types_supported: ['authorization_code'],
    code_challenge_methods_supported: ['S256'],
    token_endpoint_auth_methods_supported: ['none'],
    authorization_response_iss_parameter_supported: true,
    scopes_supported: OAUTH_SCOPES
  });
});

app.get('/oauth/authorize', (req, res) => {
  const {
    client_id: clientId,
    redirect_uri: redirectUri,
    response_type: responseType,
    code_challenge: codeChallenge,
    code_challenge_method: codeChallengeMethod,
    state,
    scope,
    resource
  } = req.query;
  const base = requestBaseUrl(req);
  const expectedResource = `${base}/mcp`;

  if (responseType !== 'code' || clientId !== CLIENT_ID || !allowedRedirect(redirectUri)) {
    return res.status(400).type('html').send('<h1>Solicitação OAuth inválida</h1>');
  }
  if (!codeChallenge || codeChallengeMethod !== 'S256') {
    return res.status(400).type('html').send('<h1>PKCE S256 é obrigatório</h1>');
  }
  if (resource && resource !== expectedResource) {
    return res.status(400).type('html').send('<h1>Resource OAuth inválido</h1>');
  }

  const hidden = {
    client_id: clientId,
    redirect_uri: redirectUri,
    response_type: 'code',
    code_challenge: codeChallenge,
    code_challenge_method: 'S256',
    state: state || '',
    scope: requestedScopes(scope).join(' '),
    resource: expectedResource
  };
  const hiddenInputs = Object.entries(hidden)
    .map(([key, value]) => `<input type="hidden" name="${escapeHtml(key)}" value="${escapeHtml(value)}">`)
    .join('');

  res.type('html').send(`<!doctype html><html lang="pt-BR"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Conectar Meu Discord</title><style>body{font-family:Arial,sans-serif;background:#eef0ff;margin:0;min-height:100vh;display:grid;place-items:center;padding:24px}.card{max-width:460px;width:100%;background:#fff;padding:30px;border-radius:20px;box-shadow:0 10px 35px #0001}input{box-sizing:border-box;width:100%;padding:12px;margin:8px 0 16px;border:1px solid #cfd3ef;border-radius:10px;font-size:16px}button{width:100%;padding:12px;border:0;border-radius:10px;background:#5865f2;color:#fff;font-size:16px;font-weight:700}.muted{color:#667;font-size:14px;line-height:1.5}</style></head><body><main class="card"><h1>Conectar Meu Discord</h1><p>Autorize o ChatGPT a acessar a ponte privada do seu Discord.</p><form method="post" action="/oauth/authorize">${hiddenInputs}<label>Chave privada</label><input type="password" name="access_key" autocomplete="current-password" required><button type="submit">Autorizar ChatGPT</button></form><p class="muted">Digite o valor de MCP_LOGIN_SECRET configurado no Railway. O token do Discord não é digitado aqui.</p></main></body></html>`);
});

app.post('/oauth/authorize', express.urlencoded({ extended: false, limit: '64kb' }), (req, res) => {
  if (!LOGIN_SECRET) return res.status(503).type('html').send('<h1>Autenticação não configurada</h1>');
  const body = req.body || {};
  const base = requestBaseUrl(req);
  if (body.client_id !== CLIENT_ID || !allowedRedirect(body.redirect_uri)) {
    return res.status(400).type('html').send('<h1>Cliente OAuth inválido</h1>');
  }
  if (!safeEqual(body.access_key, LOGIN_SECRET)) {
    return res.status(401).type('html').send('<h1>Chave inválida</h1><p>Volte e tente novamente.</p>');
  }

  const code = randomToken(32);
  oauthCodes.set(code, {
    clientId: body.client_id,
    redirectUri: body.redirect_uri,
    codeChallenge: body.code_challenge,
    scope: requestedScopes(body.scope).join(' '),
    resource: body.resource || `${base}/mcp`,
    expiresAt: Date.now() + AUTH_CODE_TTL_MS
  });
  const redirect = new URL(body.redirect_uri);
  redirect.searchParams.set('code', code);
  if (body.state) redirect.searchParams.set('state', body.state);
  redirect.searchParams.set('iss', base);
  res.redirect(302, redirect.toString());
});

app.post('/oauth/token', express.urlencoded({ extended: false, limit: '64kb' }), (req, res) => {
  const body = req.body || {};
  if (body.grant_type !== 'authorization_code') return res.status(400).json({ error: 'unsupported_grant_type' });
  const record = oauthCodes.get(body.code);
  oauthCodes.delete(body.code);
  if (!record || record.expiresAt < Date.now()) return res.status(400).json({ error: 'invalid_grant' });
  if (record.clientId !== body.client_id || record.redirectUri !== body.redirect_uri) return res.status(400).json({ error: 'invalid_grant' });
  if (body.resource && body.resource !== record.resource) return res.status(400).json({ error: 'invalid_target' });

  const verifier = String(body.code_verifier || '');
  const verifierHash = crypto.createHash('sha256').update(verifier).digest('base64url');
  if (!verifier || !safeEqual(verifierHash, record.codeChallenge)) {
    return res.status(400).json({ error: 'invalid_grant', error_description: 'PKCE validation failed' });
  }

  const now = Math.floor(Date.now() / 1000);
  const base = requestBaseUrl(req);
  const token = signAccessToken({
    sub: 'personal-discord-owner',
    iss: base,
    aud: record.resource,
    iat: now,
    exp: now + ACCESS_TOKEN_TTL_SECONDS,
    scope: record.scope
  }, LOGIN_SECRET);
  res.set('Cache-Control', 'no-store');
  res.set('Pragma', 'no-cache');
  res.json({ access_token: token, token_type: 'Bearer', expires_in: ACCESS_TOKEN_TTL_SECONDS, scope: record.scope });
});

app.use('/mcp', (req, _res, next) => {
  if (req.method === 'POST') {
    req.headers['content-type'] = 'application/json';
    const accepts = String(req.headers.accept || '').split(',').map(value => value.trim()).filter(Boolean);
    if (!accepts.some(value => value.toLowerCase().startsWith('application/json'))) accepts.push('application/json');
    if (!accepts.some(value => value.toLowerCase().startsWith('text/event-stream'))) accepts.push('text/event-stream');
    req.headers.accept = accepts.join(', ');
  }
  next();
});

const parseMcpJson = express.json({ limit: '20mb', type: () => true });
app.all('/mcp', parseMcpJson, (req, res) => {
  const base = requestBaseUrl(req);
  const resource = `${base}/mcp`;
  const metadataUrl = `${base}/.well-known/oauth-protected-resource`;
  const auth = String(req.headers.authorization || '');
  const token = auth.startsWith('Bearer ') ? auth.slice(7) : '';
  const payload = token ? verifyAccessToken(token, LOGIN_SECRET, base, resource) : null;
  requestContext.run({ payload, metadataUrl, resource, issuer: base }, () => {
    void mcpNodeHandler(req, res, req.body);
  });
});

app.get('/mcp-info', (req, res) => {
  const base = requestBaseUrl(req);
  res.json({
    name: 'Meu Discord MCP',
    version: '1.0.0',
    mcp: `${base}/mcp`,
    transport: 'streamable-http',
    authentication: 'oauth2-pkce-tool-level',
    clientId: CLIENT_ID,
    authorization: `${base}/oauth/authorize`,
    token: `${base}/oauth/token`,
    callback: STABLE_CHATGPT_REDIRECT,
    scopes: OAUTH_SCOPES,
    tools: [
      'discord_status',
      'list_discord_chats',
      'read_discord_messages',
      'read_discord_audio',
      'search_discord_messages',
      'discord_cache_stats',
      'send_discord_message',
      'react_discord_message',
      'send_discord_image'
    ]
  });
});

app.listen(PUBLIC_PORT, '0.0.0.0', () => {
  console.log(`[MeuDiscord] MCP listening on 0.0.0.0:${PUBLIC_PORT}`);
  console.log(`[MeuDiscord] Bridge: ${BRIDGE_BASE}`);
  console.log(`[MeuDiscord] MCP endpoint: /mcp`);
});

async function cleanup() {
  shuttingDown = true;
  if (pythonRestartTimer) clearTimeout(pythonRestartTimer);
  try { await mcpHandler.close(); } catch (_) {}
  try { python?.kill('SIGTERM'); } catch (_) {}
}

process.once('SIGTERM', cleanup);
process.once('SIGINT', cleanup);
