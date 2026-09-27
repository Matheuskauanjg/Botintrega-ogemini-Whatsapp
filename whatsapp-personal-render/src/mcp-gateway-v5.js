import crypto from 'node:crypto';
import http from 'node:http';
import { AsyncLocalStorage } from 'node:async_hooks';
import express from 'express';
import { createMcpHandler, McpServer } from '@modelcontextprotocol/server';
import { toNodeHandler } from '@modelcontextprotocol/node';
import * as z from 'zod/v4';

const ACCESS_TOKEN_TTL_SECONDS = 60 * 60 * 24 * 30;
const AUTH_CODE_TTL_MS = 5 * 60 * 1000;
const OAUTH_SCOPES = ['whatsapp.read', 'whatsapp.send'];
const DEFAULT_CLIENT_ID = 'chatgpt-meu-whatsapp';
const STABLE_CHATGPT_REDIRECT = 'https://chatgpt.com/connector_platform_oauth_redirect';

const oauthCodes = new Map();
const requestContext = new AsyncLocalStorage();

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
    if (payload.iss !== expectedIssuer) return null;
    if (payload.aud !== expectedResource) return null;
    return payload;
  } catch {
    return null;
  }
}

function requestBaseUrl(req) {
  const configured = String(process.env.PUBLIC_BASE_URL || '').replace(/\/$/, '');
  if (configured) return configured;
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

export async function startMcpGateway({ publicPort, bridgePort, audioPort }) {
  const API_TOKEN = process.env.API_TOKEN || '';
  const LOGIN_SECRET = process.env.MCP_LOGIN_SECRET || API_TOKEN || process.env.QR_SECRET || '';
  const CLIENT_ID = process.env.MCP_CLIENT_ID || DEFAULT_CLIENT_ID;
  const BRIDGE_BASE = `http://127.0.0.1:${bridgePort}`;
  const AUDIO_BASE = `http://127.0.0.1:${audioPort}`;

  async function internalJson(pathname, options = {}, target = 'bridge') {
    if (!API_TOKEN) throw new Error('API_TOKEN is not configured.');
    const headers = new Headers(options.headers || {});
    headers.set('authorization', `Bearer ${API_TOKEN}`);
    if (options.body && !headers.has('content-type')) headers.set('content-type', 'application/json');
    const base = target === 'audio' ? AUDIO_BASE : BRIDGE_BASE;
    const startedAt = performance.now();
    const response = await fetch(`${base}${pathname}`, { ...options, headers });
    const text = await response.text();
    let data;
    try { data = text ? JSON.parse(text) : {}; } catch { data = { raw: text }; }
    if (!response.ok) throw new Error(data?.error || `Internal WhatsApp API returned HTTP ${response.status}`);
    if (data && typeof data === 'object' && !Array.isArray(data) && data.internalGatewayLatencyMs == null) {
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
    const metadataUrl = ctx.metadataUrl || `${String(process.env.PUBLIC_BASE_URL || '').replace(/\/$/, '')}/.well-known/oauth-protected-resource`;
    const challenge = `Bearer resource_metadata=\"${metadataUrl}\", scope=\"${requiredScopes.join(' ')}\", error=\"insufficient_scope\", error_description=\"Connect your WhatsApp account to continue\"`;
    return {
      content: [{ type: 'text', text: 'Authentication required: connect your WhatsApp account to continue.' }],
      _meta: { 'mcp/www_authenticate': [challenge] },
      isError: true
    };
  }

  function requireToolAuth(requiredScopes) {
    const ctx = requestContext.getStore() || {};
    if (!LOGIN_SECRET || !hasScopes(ctx.payload, requiredScopes)) return authFailure(requiredScopes);
    return null;
  }

  function createWhatsappMcpServer() {
    const server = new McpServer({ name: 'meu-whatsapp', version: '2.3.0' });

    server.registerTool('whatsapp_status', {
      title: 'Status do WhatsApp',
      description: 'Verifica conexão, cache e armazenamento persistente do WhatsApp pessoal.',
      inputSchema: z.object({}),
      ...authDescriptor(['whatsapp.read']),
      annotations: { readOnlyHint: true, destructiveHint: false, openWorldHint: false }
    }, async () => {
      const denied = requireToolAuth(['whatsapp.read']);
      if (denied) return denied;
      try { return textResult(await internalJson('/api/status')); }
      catch (error) { return errorResult(error.message); }
    });

    server.registerTool('list_whatsapp_chats', {
      title: 'Listar conversas do WhatsApp',
      description: 'Lista conversas recentes, incluindo as persistidas no SQLite.',
      inputSchema: z.object({ limit: z.number().int().min(1).max(100).default(30) }),
      ...authDescriptor(['whatsapp.read']),
      annotations: { readOnlyHint: true, destructiveHint: false, openWorldHint: false }
    }, async ({ limit }) => {
      const denied = requireToolAuth(['whatsapp.read']);
      if (denied) return denied;
      try { return textResult(await internalJson(`/api/chats?limit=${encodeURIComponent(limit)}`)); }
      catch (error) { return errorResult(error.message); }
    });

    server.registerTool('read_whatsapp_messages', {
      title: 'Ler mensagens do WhatsApp',
      description: 'Lê mensagens persistidas de uma conversa. Retorna messageId em id; use esse id para reply, reação ou mencionar o autor.',
      inputSchema: z.object({
        chatId: z.string().min(1),
        limit: z.number().int().min(1).max(100).default(30)
      }),
      ...authDescriptor(['whatsapp.read']),
      annotations: { readOnlyHint: true, destructiveHint: false, openWorldHint: false }
    }, async ({ chatId, limit }) => {
      const denied = requireToolAuth(['whatsapp.read']);
      if (denied) return denied;
      try { return textResult(await internalJson(`/api/chats/${encodeURIComponent(chatId)}/messages?limit=${encodeURIComponent(limit)}`)); }
      catch (error) { return errorResult(error.message); }
    });

    server.registerTool('read_whatsapp_audio', {
      title: 'Ouvir áudio do WhatsApp',
      description: 'Baixa e transcreve uma mensagem de voz recente. Use chatId e messageId obtidos em read_whatsapp_messages.',
      inputSchema: z.object({
        chatId: z.string().min(1),
        messageId: z.string().min(1)
      }),
      ...authDescriptor(['whatsapp.read']),
      annotations: { readOnlyHint: true, destructiveHint: false, openWorldHint: false }
    }, async ({ chatId, messageId }) => {
      const denied = requireToolAuth(['whatsapp.read']);
      if (denied) return denied;
      try {
        const data = await internalJson('/api/audio', {
          method: 'POST',
          body: JSON.stringify({ chatId, messageId })
        }, 'audio');
        return audioResult(data);
      } catch (error) {
        return errorResult(error.message);
      }
    });

    server.registerTool('search_whatsapp_messages', {
      title: 'Pesquisar mensagens do WhatsApp',
      description: 'Pesquisa texto no histórico persistente SQLite do WhatsApp.',
      inputSchema: z.object({ query: z.string().min(1).max(500) }),
      ...authDescriptor(['whatsapp.read']),
      annotations: { readOnlyHint: true, destructiveHint: false, openWorldHint: false }
    }, async ({ query }) => {
      const denied = requireToolAuth(['whatsapp.read']);
      if (denied) return denied;
      try { return textResult(await internalJson(`/api/search?q=${encodeURIComponent(query)}`)); }
      catch (error) { return errorResult(error.message); }
    });

    server.registerTool('whatsapp_storage_stats', {
      title: 'Estatísticas do histórico do WhatsApp',
      description: 'Mostra quantas conversas, mensagens e mapeamentos LID estão persistidos.',
      inputSchema: z.object({}),
      ...authDescriptor(['whatsapp.read']),
      annotations: { readOnlyHint: true, destructiveHint: false, openWorldHint: false }
    }, async () => {
      const denied = requireToolAuth(['whatsapp.read']);
      if (denied) return denied;
      try { return textResult(await internalJson('/api/db-stats')); }
      catch (error) { return errorResult(error.message); }
    });

    server.registerTool('send_whatsapp_message', {
      title: 'Enviar ou responder mensagem no WhatsApp',
      description: 'Envia texto. Pode responder/citar uma mensagem específica e mencionar usuários. Para responder e marcar o autor, use replyToMessageId e mentionAuthorOfMessageId com o mesmo id.',
      inputSchema: z.object({
        to: z.string().min(1).describe('Número com DDI ou JID da conversa, inclusive @g.us e @lid.'),
        message: z.string().min(1).max(5000),
        replyToMessageId: z.string().min(1).optional().describe('ID da mensagem que deve aparecer citada na resposta.'),
        mentionJids: z.array(z.string().min(1)).max(20).optional().describe('Números/JIDs a mencionar com @.'),
        mentionAuthorOfMessageId: z.string().min(1).optional().describe('ID de uma mensagem cujo autor deve ser mencionado automaticamente.'),
        prependMentions: z.boolean().optional().default(true).describe('Insere visualmente @numero no início para as menções.')
      }),
      ...authDescriptor(['whatsapp.send']),
      annotations: { readOnlyHint: false, destructiveHint: false, openWorldHint: false }
    }, async ({ to, message, replyToMessageId, mentionJids, mentionAuthorOfMessageId, prependMentions }) => {
      const denied = requireToolAuth(['whatsapp.send']);
      if (denied) return denied;
      try {
        return textResult(await internalJson('/api/send', {
          method: 'POST',
          body: JSON.stringify({ to, message, replyToMessageId, mentionJids, mentionAuthorOfMessageId, prependMentions })
        }));
      } catch (error) {
        return errorResult(error.message);
      }
    });

    server.registerTool('react_whatsapp_message', {
      title: 'Reagir a uma mensagem do WhatsApp',
      description: 'Adiciona uma reação por emoji a uma mensagem específica. Use emoji vazio para remover a reação.',
      inputSchema: z.object({
        to: z.string().min(1).describe('JID da conversa que contém a mensagem.'),
        messageId: z.string().min(1).describe('ID retornado por read_whatsapp_messages.'),
        emoji: z.string().max(20).default('')
      }),
      ...authDescriptor(['whatsapp.send']),
      annotations: { readOnlyHint: false, destructiveHint: false, openWorldHint: false }
    }, async ({ to, messageId, emoji }) => {
      const denied = requireToolAuth(['whatsapp.send']);
      if (denied) return denied;
      try {
        return textResult(await internalJson('/api/react', {
          method: 'POST',
          body: JSON.stringify({ to, messageId, emoji })
        }));
      } catch (error) {
        return errorResult(error.message);
      }
    });

    server.registerTool('send_whatsapp_image', {
      title: 'Enviar imagem no WhatsApp',
      description: 'Envia imagem normalizada para JPEG; também pode citar uma mensagem e mencionar usuários.',
      inputSchema: z.object({
        to: z.string().min(1),
        imageUrl: z.string().url().optional(),
        imageBase64: z.string().optional(),
        mimetype: z.string().optional(),
        caption: z.string().max(5000).optional(),
        replyToMessageId: z.string().min(1).optional(),
        mentionJids: z.array(z.string().min(1)).max(20).optional(),
        prependMentions: z.boolean().optional().default(true)
      }),
      ...authDescriptor(['whatsapp.send']),
      annotations: { readOnlyHint: false, destructiveHint: false, openWorldHint: true }
    }, async ({ to, imageUrl, imageBase64, mimetype, caption, replyToMessageId, mentionJids, prependMentions }) => {
      const denied = requireToolAuth(['whatsapp.send']);
      if (denied) return denied;
      if (!imageUrl && !imageBase64) return errorResult('imageUrl or imageBase64 is required');
      try {
        return textResult(await internalJson('/api/send-image', {
          method: 'POST',
          body: JSON.stringify({ to, imageUrl, imageBase64, mimetype, caption, replyToMessageId, mentionJids, prependMentions })
        }));
      } catch (error) {
        return errorResult(error.message);
      }
    });

    return server;
  }

  const mcpHandler = createMcpHandler(createWhatsappMcpServer);
  const mcpNodeHandler = toNodeHandler(mcpHandler, {
    onerror(error) { console.error('[MCP] Adapter error:', error); }
  });

  const app = express();
  app.set('trust proxy', true);

  app.get('/.well-known/oauth-protected-resource', (req, res) => {
    const base = requestBaseUrl(req);
    res.json({ resource: `${base}/mcp`, authorization_servers: [base], scopes_supported: OAUTH_SCOPES, resource_documentation: `${base}/mcp-info` });
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
    console.log(`[OAuth] authorize client=${String(clientId || '')} redirect=${String(redirectUri || '')}`);

    if (responseType !== 'code' || clientId !== CLIENT_ID || !allowedRedirect(redirectUri)) {
      return res.status(400).type('html').send('<h1>Solicitação OAuth inválida</h1>');
    }
    if (!codeChallenge || codeChallengeMethod !== 'S256') return res.status(400).type('html').send('<h1>PKCE S256 é obrigatório</h1>');
    if (resource && resource !== expectedResource) return res.status(400).type('html').send('<h1>Resource OAuth inválido</h1>');

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

    res.type('html').send(`<!doctype html><html lang="pt-BR"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Conectar Meu WhatsApp</title><style>body{font-family:Arial,sans-serif;background:#eef3f1;margin:0;min-height:100vh;display:grid;place-items:center;padding:24px}.card{max-width:460px;width:100%;background:#fff;padding:30px;border-radius:20px;box-shadow:0 10px 35px #0001}input{box-sizing:border-box;width:100%;padding:12px;margin:8px 0 16px;border:1px solid #ccd8d2;border-radius:10px;font-size:16px}button{width:100%;padding:12px;border:0;border-radius:10px;background:#1f8f55;color:#fff;font-size:16px;font-weight:700}.muted{color:#66766f;font-size:14px;line-height:1.5}</style></head><body><main class="card"><h1>Conectar Meu WhatsApp</h1><p>Autorize o ChatGPT a acessar o seu WhatsApp pessoal.</p><form method="post" action="/oauth/authorize">${hiddenInputs}<label>Chave privada</label><input type="password" name="access_key" autocomplete="current-password" required><button type="submit">Autorizar ChatGPT</button></form><p class="muted">Digite o valor de MCP_LOGIN_SECRET configurado no serviço.</p></main></body></html>`);
  });

  app.post('/oauth/authorize', express.urlencoded({ extended: false, limit: '64kb' }), (req, res) => {
    if (!LOGIN_SECRET) return res.status(503).type('html').send('<h1>Autenticação não configurada</h1>');
    const body = req.body || {};
    const base = requestBaseUrl(req);
    if (body.client_id !== CLIENT_ID || !allowedRedirect(body.redirect_uri)) return res.status(400).type('html').send('<h1>Cliente OAuth inválido</h1>');
    if (!safeEqual(body.access_key, LOGIN_SECRET)) return res.status(401).type('html').send('<h1>Chave inválida</h1><p>Volte e tente novamente.</p>');

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
    console.log('[OAuth] authorization code issued');
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
      sub: 'personal-whatsapp-owner',
      iss: base,
      aud: record.resource,
      iat: now,
      exp: now + ACCESS_TOKEN_TTL_SECONDS,
      scope: record.scope
    }, LOGIN_SECRET);
    console.log('[OAuth] access token issued');
    res.set('Cache-Control', 'no-store');
    res.set('Pragma', 'no-cache');
    res.json({ access_token: token, token_type: 'Bearer', expires_in: ACCESS_TOKEN_TTL_SECONDS, scope: record.scope });
  });

  app.use('/mcp', (req, _res, next) => {
    const originalContentType = String(req.headers['content-type'] || '');
    const originalAccept = String(req.headers.accept || '');
    console.log(`[MCP] inbound ${req.method} content-type="${originalContentType || '(none)'}" accept="${originalAccept || '(none)'}"`);
    if (req.method === 'POST') {
      req.headers['content-type'] = 'application/json';
      const accepts = originalAccept.split(',').map(value => value.trim()).filter(Boolean);
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
    const method = String(req.body?.method || req.get('Mcp-Method') || req.method);
    res.once('finish', () => {
      const responseContentType = String(res.getHeader('content-type') || '');
      console.log(`[MCP] ${method} -> HTTP ${res.statusCode} ${responseContentType}`);
    });
    requestContext.run({ payload, metadataUrl, resource, issuer: base }, () => {
      void mcpNodeHandler(req, res, req.body);
    });
  });

  app.get('/mcp-info', (req, res) => {
    const base = requestBaseUrl(req);
    res.json({
      name: 'Meu WhatsApp MCP',
      version: '2.3.0',
      mcp: `${base}/mcp`,
      transport: 'streamable-http',
      authentication: 'oauth2-pkce-tool-level',
      clientId: CLIENT_ID,
      authorization: `${base}/oauth/authorize`,
      token: `${base}/oauth/token`,
      callback: STABLE_CHATGPT_REDIRECT,
      scopes: OAUTH_SCOPES,
      tools: [
        'whatsapp_status',
        'list_whatsapp_chats',
        'read_whatsapp_messages',
        'read_whatsapp_audio',
        'search_whatsapp_messages',
        'whatsapp_storage_stats',
        'send_whatsapp_message',
        'react_whatsapp_message',
        'send_whatsapp_image'
      ]
    });
  });

  app.use((req, res) => {
    const targetPort = req.path?.startsWith('/media/audio/') ? audioPort : bridgePort;
    const headers = { ...req.headers, host: `127.0.0.1:${targetPort}` };
    delete headers['content-length'];
    delete headers.connection;
    const upstream = http.request({ hostname: '127.0.0.1', port: targetPort, method: req.method, path: req.originalUrl, headers }, upstreamRes => {
      res.status(upstreamRes.statusCode || 502);
      for (const [key, value] of Object.entries(upstreamRes.headers)) if (value !== undefined) res.setHeader(key, value);
      upstreamRes.pipe(res);
    });
    upstream.on('error', error => {
      console.error('[Gateway] Proxy error:', error);
      if (!res.headersSent) res.status(502).json({ error: 'WhatsApp bridge unavailable' });
      else res.end();
    });
    req.pipe(upstream);
  });

  const publicServer = app.listen(publicPort, '0.0.0.0', () => {
    console.log(`[Gateway] Public HTTP/MCP listening on 0.0.0.0:${publicPort}`);
    console.log('[MCP] Endpoint: /mcp (Streamable HTTP + persistent history + replies/mentions/reactions)');
    console.log(`[OAuth] Client ID: ${CLIENT_ID}`);
    console.log(`[OAuth] Callback: ${STABLE_CHATGPT_REDIRECT}`);
    console.log(`[Gateway] Direct bridge: ${BRIDGE_BASE}`);
    console.log(`[Gateway] Audio proxy: ${AUDIO_BASE}`);
  });

  const cleanup = async () => {
    try { await mcpHandler.close(); } catch (_) {}
    try { publicServer.close(); } catch (_) {}
  };
  process.once('SIGTERM', cleanup);
  process.once('SIGINT', cleanup);
  return { app, publicServer, mcpHandler };
}
