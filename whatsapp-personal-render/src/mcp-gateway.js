import crypto from 'node:crypto';
import http from 'node:http';
import express from 'express';
import { createMcpHandler, McpServer } from '@modelcontextprotocol/server';
import { toNodeHandler } from '@modelcontextprotocol/node';
import * as z from 'zod/v4';

const ACCESS_TOKEN_TTL_SECONDS = 60 * 60 * 24 * 30;
const AUTH_CODE_TTL_MS = 5 * 60 * 1000;
const OAUTH_SCOPES = ['whatsapp.read', 'whatsapp.send'];

const oauthClients = new Map();
const oauthCodes = new Map();

function base64url(value) {
  return Buffer.from(value).toString('base64url');
}

function escapeHtml(value) {
  return String(value ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
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

function verifyAccessToken(token, secret, expectedResource) {
  if (!token || !secret || !token.includes('.')) return null;
  const [body, signature] = token.split('.');
  if (!body || !signature || !safeEqual(signature, hmac(body, secret))) return null;

  try {
    const payload = JSON.parse(Buffer.from(body, 'base64url').toString('utf8'));
    if (!payload.exp || payload.exp <= Math.floor(Date.now() / 1000)) return null;
    if (payload.resource && payload.resource !== expectedResource) return null;
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

function allowedRedirect(clientId, redirectUri) {
  try {
    const parsed = new URL(redirectUri);
    if (parsed.protocol !== 'https:') return false;

    const registered = oauthClients.get(clientId);
    if (registered?.redirect_uris?.includes(redirectUri)) return true;

    return parsed.hostname === 'chatgpt.com'
      || parsed.hostname.endsWith('.chatgpt.com')
      || parsed.hostname === 'chat.openai.com'
      || parsed.hostname.endsWith('.openai.com');
  } catch {
    return false;
  }
}

function requestedScopes(scopeText) {
  const requested = String(scopeText || OAUTH_SCOPES.join(' '))
    .split(/\s+/)
    .filter(Boolean);
  return requested.filter(scope => OAUTH_SCOPES.includes(scope));
}

function result(value) {
  const structured = { result: value };
  return {
    content: [{ type: 'text', text: JSON.stringify(value, null, 2) }],
    structuredContent: structured
  };
}

function errorResult(message) {
  return {
    content: [{ type: 'text', text: String(message) }],
    isError: true
  };
}

export async function startMcpGateway({ publicPort, internalPort }) {
  const API_TOKEN = process.env.API_TOKEN || '';
  const LOGIN_SECRET = process.env.MCP_LOGIN_SECRET || API_TOKEN || process.env.QR_SECRET || '';
  const INTERNAL_BASE = `http://127.0.0.1:${internalPort}`;

  async function internalJson(pathname, options = {}) {
    if (!API_TOKEN) {
      throw new Error('API_TOKEN is not configured on Render. Configure API_TOKEN before using the MCP plugin.');
    }

    const headers = new Headers(options.headers || {});
    headers.set('authorization', `Bearer ${API_TOKEN}`);
    if (options.body && !headers.has('content-type')) headers.set('content-type', 'application/json');

    const response = await fetch(`${INTERNAL_BASE}${pathname}`, {
      ...options,
      headers
    });

    const text = await response.text();
    let data;
    try { data = text ? JSON.parse(text) : {}; } catch { data = { raw: text }; }
    if (!response.ok) {
      throw new Error(data?.error || `Internal WhatsApp API returned HTTP ${response.status}`);
    }
    return data;
  }

  function createWhatsappMcpServer() {
    const server = new McpServer({
      name: 'meu-whatsapp',
      version: '1.0.0'
    });

    server.registerTool(
      'whatsapp_status',
      {
        title: 'Status do WhatsApp',
        description: 'Verifica se o WhatsApp pessoal está conectado e pronto para uso.',
        inputSchema: z.object({}),
        securitySchemes: [{ type: 'oauth2', scopes: ['whatsapp.read'] }],
        annotations: { readOnlyHint: true, destructiveHint: false, openWorldHint: false }
      },
      async () => {
        try { return result(await internalJson('/api/status')); }
        catch (error) { return errorResult(error.message); }
      }
    );

    server.registerTool(
      'list_whatsapp_chats',
      {
        title: 'Listar conversas do WhatsApp',
        description: 'Lista conversas recentes armazenadas pelo bridge pessoal do WhatsApp.',
        inputSchema: z.object({ limit: z.number().int().min(1).max(100).default(30) }),
        securitySchemes: [{ type: 'oauth2', scopes: ['whatsapp.read'] }],
        annotations: { readOnlyHint: true, destructiveHint: false, openWorldHint: false }
      },
      async ({ limit }) => {
        try { return result(await internalJson(`/api/chats?limit=${encodeURIComponent(limit)}`)); }
        catch (error) { return errorResult(error.message); }
      }
    );

    server.registerTool(
      'read_whatsapp_messages',
      {
        title: 'Ler mensagens do WhatsApp',
        description: 'Lê as mensagens recentes em cache de uma conversa específica pelo chatId.',
        inputSchema: z.object({
          chatId: z.string().min(1),
          limit: z.number().int().min(1).max(100).default(30)
        }),
        securitySchemes: [{ type: 'oauth2', scopes: ['whatsapp.read'] }],
        annotations: { readOnlyHint: true, destructiveHint: false, openWorldHint: false }
      },
      async ({ chatId, limit }) => {
        try {
          return result(await internalJson(`/api/chats/${encodeURIComponent(chatId)}/messages?limit=${encodeURIComponent(limit)}`));
        } catch (error) { return errorResult(error.message); }
      }
    );

    server.registerTool(
      'search_whatsapp_messages',
      {
        title: 'Pesquisar mensagens do WhatsApp',
        description: 'Pesquisa texto nas mensagens recentes armazenadas em cache pelo bridge.',
        inputSchema: z.object({ query: z.string().min(1).max(500) }),
        securitySchemes: [{ type: 'oauth2', scopes: ['whatsapp.read'] }],
        annotations: { readOnlyHint: true, destructiveHint: false, openWorldHint: false }
      },
      async ({ query }) => {
        try { return result(await internalJson(`/api/search?q=${encodeURIComponent(query)}`)); }
        catch (error) { return errorResult(error.message); }
      }
    );

    server.registerTool(
      'send_whatsapp_message',
      {
        title: 'Enviar mensagem no WhatsApp',
        description: 'Envia uma única mensagem de texto pelo WhatsApp pessoal. Use somente quando o usuário pedir explicitamente para enviar.',
        inputSchema: z.object({
          to: z.string().min(1).describe('Número com DDI, por exemplo 5541999999999, ou JID do WhatsApp.'),
          message: z.string().min(1).max(5000)
        }),
        securitySchemes: [{ type: 'oauth2', scopes: ['whatsapp.send'] }],
        annotations: { readOnlyHint: false, destructiveHint: false, openWorldHint: false }
      },
      async ({ to, message }) => {
        try {
          return result(await internalJson('/api/send', {
            method: 'POST',
            body: JSON.stringify({ to, message })
          }));
        } catch (error) { return errorResult(error.message); }
      }
    );

    return server;
  }

  const mcpHandler = createMcpHandler(createWhatsappMcpServer, { responseMode: 'json' });
  const mcpNodeHandler = toNodeHandler(mcpHandler, {
    onerror(error) {
      console.error('[MCP] Adapter error:', error);
    }
  });

  const app = express();
  app.set('trust proxy', true);

  app.get('/.well-known/oauth-protected-resource', (req, res) => {
    const base = requestBaseUrl(req);
    res.json({
      resource: `${base}/mcp`,
      authorization_servers: [base],
      scopes_supported: OAUTH_SCOPES,
      resource_documentation: `${base}/`
    });
  });

  app.get('/.well-known/oauth-authorization-server', (req, res) => {
    const base = requestBaseUrl(req);
    res.json({
      issuer: base,
      authorization_endpoint: `${base}/oauth/authorize`,
      token_endpoint: `${base}/oauth/token`,
      registration_endpoint: `${base}/oauth/register`,
      response_types_supported: ['code'],
      grant_types_supported: ['authorization_code'],
      code_challenge_methods_supported: ['S256'],
      token_endpoint_auth_methods_supported: ['none'],
      scopes_supported: OAUTH_SCOPES
    });
  });

  app.post('/oauth/register', express.json({ limit: '64kb' }), (req, res) => {
    const redirectUris = Array.isArray(req.body?.redirect_uris) ? req.body.redirect_uris : [];
    if (!redirectUris.length || redirectUris.some(uri => !allowedRedirect('__new__', uri))) {
      return res.status(400).json({ error: 'invalid_redirect_uri' });
    }

    const clientId = `chatgpt_${randomToken(18)}`;
    oauthClients.set(clientId, {
      redirect_uris: redirectUris,
      client_name: req.body?.client_name || 'ChatGPT',
      created_at: Date.now()
    });

    res.status(201).json({
      client_id: clientId,
      client_id_issued_at: Math.floor(Date.now() / 1000),
      redirect_uris: redirectUris,
      token_endpoint_auth_method: 'none',
      grant_types: ['authorization_code'],
      response_types: ['code']
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

    if (responseType !== 'code' || !clientId || !redirectUri || !allowedRedirect(clientId, redirectUri)) {
      return res.status(400).type('html').send('<h1>Solicitação OAuth inválida</h1>');
    }
    if (!codeChallenge || codeChallengeMethod !== 'S256') {
      return res.status(400).type('html').send('<h1>PKCE S256 é obrigatório</h1>');
    }

    const base = requestBaseUrl(req);
    const expectedResource = `${base}/mcp`;
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

    res.type('html').send(`<!doctype html>
<html lang="pt-BR"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Conectar Meu WhatsApp</title>
<style>body{font-family:Arial,sans-serif;background:#eef3f1;margin:0;min-height:100vh;display:grid;place-items:center;padding:24px}.card{max-width:460px;width:100%;background:white;padding:30px;border-radius:20px;box-shadow:0 10px 35px #0001}h1{margin-top:0}input{width:100%;padding:12px;margin:8px 0 16px;border:1px solid #ccd8d2;border-radius:10px;font-size:16px}button{width:100%;padding:12px;border:0;border-radius:10px;background:#1f8f55;color:white;font-size:16px;font-weight:700}.muted{color:#66766f;font-size:14px;line-height:1.5}</style>
</head><body><main class="card"><h1>Conectar Meu WhatsApp</h1><p>Autorize o ChatGPT a acessar o seu bridge pessoal do WhatsApp.</p><form method="post" action="/oauth/authorize">${hiddenInputs}<label>Chave privada do Render</label><input type="password" name="access_key" autocomplete="current-password" required><button type="submit">Autorizar ChatGPT</button></form><p class="muted">Use MCP_LOGIN_SECRET; se não estiver configurado, use API_TOKEN ou QR_SECRET do serviço. A chave é validada neste servidor e não é enviada no redirecionamento.</p></main></body></html>`);
  });

  app.post('/oauth/authorize', express.urlencoded({ extended: false, limit: '64kb' }), (req, res) => {
    if (!LOGIN_SECRET) {
      return res.status(503).type('html').send('<h1>Autenticação não configurada</h1><p>Configure MCP_LOGIN_SECRET, API_TOKEN ou QR_SECRET no Render.</p>');
    }

    const body = req.body || {};
    if (!safeEqual(body.access_key, LOGIN_SECRET)) {
      return res.status(401).type('html').send('<h1>Chave inválida</h1><p>Volte e tente novamente.</p>');
    }
    if (!body.client_id || !body.redirect_uri || !allowedRedirect(body.client_id, body.redirect_uri)) {
      return res.status(400).type('html').send('<h1>Cliente OAuth inválido</h1>');
    }

    const code = randomToken(32);
    oauthCodes.set(code, {
      clientId: body.client_id,
      redirectUri: body.redirect_uri,
      codeChallenge: body.code_challenge,
      scope: requestedScopes(body.scope).join(' '),
      resource: body.resource,
      expiresAt: Date.now() + AUTH_CODE_TTL_MS
    });

    const redirect = new URL(body.redirect_uri);
    redirect.searchParams.set('code', code);
    if (body.state) redirect.searchParams.set('state', body.state);
    res.redirect(302, redirect.toString());
  });

  app.post('/oauth/token', express.urlencoded({ extended: false, limit: '64kb' }), (req, res) => {
    const body = req.body || {};
    if (body.grant_type !== 'authorization_code') {
      return res.status(400).json({ error: 'unsupported_grant_type' });
    }

    const record = oauthCodes.get(body.code);
    oauthCodes.delete(body.code);
    if (!record || record.expiresAt < Date.now()) {
      return res.status(400).json({ error: 'invalid_grant' });
    }
    if (record.clientId !== body.client_id || record.redirectUri !== body.redirect_uri) {
      return res.status(400).json({ error: 'invalid_grant' });
    }

    const verifier = String(body.code_verifier || '');
    const verifierHash = crypto.createHash('sha256').update(verifier).digest('base64url');
    if (!verifier || !safeEqual(verifierHash, record.codeChallenge)) {
      return res.status(400).json({ error: 'invalid_grant', error_description: 'PKCE validation failed' });
    }

    const now = Math.floor(Date.now() / 1000);
    const token = signAccessToken({
      sub: 'personal-whatsapp-owner',
      iat: now,
      exp: now + ACCESS_TOKEN_TTL_SECONDS,
      scope: record.scope,
      resource: record.resource
    }, LOGIN_SECRET);

    res.json({
      access_token: token,
      token_type: 'Bearer',
      expires_in: ACCESS_TOKEN_TTL_SECONDS,
      scope: record.scope
    });
  });

  function requireMcpOAuth(req, res, next) {
    const base = requestBaseUrl(req);
    const resource = `${base}/mcp`;
    const metadata = `${base}/.well-known/oauth-protected-resource`;
    const challenge = `Bearer resource_metadata="${metadata}", scope="${OAUTH_SCOPES.join(' ')}"`;

    if (!LOGIN_SECRET) {
      res.set('WWW-Authenticate', challenge);
      return res.status(503).json({ error: 'MCP authentication is not configured on Render' });
    }

    const auth = String(req.headers.authorization || '');
    const token = auth.startsWith('Bearer ') ? auth.slice(7) : '';
    const payload = verifyAccessToken(token, LOGIN_SECRET, resource);
    if (!payload) {
      res.set('WWW-Authenticate', challenge);
      return res.status(401).json({ error: 'unauthorized' });
    }

    req.mcpAuth = payload;
    next();
  }

  const parseMcpJson = express.json({ limit: '4mb', type: ['application/json', 'application/*+json'] });
  app.all('/mcp', parseMcpJson, requireMcpOAuth, (req, res) => {
    void mcpNodeHandler(req, res, req.body);
  });

  app.get('/mcp-info', (req, res) => {
    const base = requestBaseUrl(req);
    res.json({
      name: 'Meu WhatsApp MCP',
      mcp: `${base}/mcp`,
      oauth: `${base}/.well-known/oauth-protected-resource`,
      tools: ['whatsapp_status', 'list_whatsapp_chats', 'read_whatsapp_messages', 'search_whatsapp_messages', 'send_whatsapp_message']
    });
  });

  app.use((req, res) => {
    const headers = { ...req.headers, host: `127.0.0.1:${internalPort}` };
    delete headers['content-length'];
    delete headers.connection;

    const upstream = http.request({
      hostname: '127.0.0.1',
      port: internalPort,
      method: req.method,
      path: req.originalUrl,
      headers
    }, upstreamRes => {
      res.status(upstreamRes.statusCode || 502);
      for (const [key, value] of Object.entries(upstreamRes.headers)) {
        if (value !== undefined) res.setHeader(key, value);
      }
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
    console.log(`[MCP] Endpoint: /mcp (OAuth protected)`);
    console.log(`[Gateway] Internal WhatsApp bridge: ${INTERNAL_BASE}`);
  });

  const cleanup = async () => {
    try { await mcpHandler.close(); } catch (_) {}
    try { publicServer.close(); } catch (_) {}
  };
  process.once('SIGTERM', cleanup);
  process.once('SIGINT', cleanup);

  return { app, publicServer, mcpHandler };
}
