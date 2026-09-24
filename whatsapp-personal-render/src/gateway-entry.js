import 'dotenv/config';

import fs from 'node:fs/promises';
import path from 'node:path';

const publicPort = Number(process.env.PORT || 10000);
const internalPort = Number(process.env.BRIDGE_INTERNAL_PORT || 10001);
const mcpGatewayPort = Number(process.env.MCP_GATEWAY_INTERNAL_PORT || 10002);

if (!process.env.API_TOKEN && process.env.QR_SECRET) {
  process.env.API_TOKEN = process.env.QR_SECRET;
  console.log('[Gateway] API_TOKEN ausente; usando QR_SECRET apenas para a API interna.');
}

const legacyAuthPath = process.env.WWEBJS_AUTH_PATH
  ? process.env.WWEBJS_AUTH_PATH.replace(/\.wwebjs_auth\/?$/, 'baileys_auth')
  : null;

const configuredAuthPath = process.env.BAILEYS_AUTH_PATH || legacyAuthPath || null;
const persistentAuthPath = '/var/data/baileys_auth';
const localAuthPath = path.resolve(process.cwd(), '.baileys_auth');
const candidates = [];

if (configuredAuthPath && !configuredAuthPath.startsWith('/tmp/')) candidates.push(configuredAuthPath);
candidates.push(persistentAuthPath);
if (configuredAuthPath) candidates.push(configuredAuthPath);
candidates.push(localAuthPath, '/tmp/baileys_auth');

async function ensureBaileysAuthPath() {
  const tried = new Set();
  for (const candidate of candidates) {
    if (!candidate || tried.has(candidate)) continue;
    tried.add(candidate);
    try {
      await fs.mkdir(candidate, { recursive: true });
      await fs.access(candidate);
      process.env.BAILEYS_AUTH_PATH = candidate;
      console.log(`[Gateway] Baileys auth path: ${candidate}`);
      if (candidate.startsWith('/tmp/')) {
        console.warn('[Gateway] Sessão em /tmp é efêmera. Anexe um Persistent Disk no Render em /var/data para manter o login entre deploys.');
      } else if (candidate === persistentAuthPath) {
        console.log('[Gateway] Sessão do WhatsApp usando armazenamento persistente em /var/data.');
      }
      return candidate;
    } catch (error) {
      console.warn(`[Gateway] Auth path indisponível: ${candidate} (${error.code || error.message})`);
    }
  }
  throw new Error('Nenhum diretório gravável disponível para a sessão do WhatsApp.');
}

await ensureBaileysAuthPath();

// 1) Bridge REST/Baileys em localhost:10001.
process.env.PORT = String(internalPort);
await import('./server-media-v2.js');

// Intercepta somente a leitura interna de /api/audio para acrescentar uma
// transcrição em texto antes que o resultado chegue ao MCP. O áudio binário
// continua sendo retornado normalmente como conteúdo MCP.
await import('./audio-transcription-fetch-patch.js');

// 2) Gateway MCP/OAuth em localhost:10002.
const { startMcpGateway } = await import('./mcp-gateway-v5.js');
await startMcpGateway({ publicPort: mcpGatewayPort, internalPort });

// 3) Proxy público na PORT do Render.
process.env.PORT = String(publicPort);
const { startPublicMcpProxy } = await import('./public-mcp-proxy.js');
startPublicMcpProxy({ publicPort, targetPort: mcpGatewayPort });
