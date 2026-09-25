import 'dotenv/config';

import fs from 'node:fs/promises';
import path from 'node:path';

const publicPort = Number(process.env.PORT || 10000);
const bridgePort = Number(process.env.BRIDGE_INTERNAL_PORT || 10001);
const mcpGatewayPort = Number(process.env.MCP_GATEWAY_INTERNAL_PORT || 10002);
const audioHandoffPort = Number(process.env.AUDIO_HANDOFF_INTERNAL_PORT || 10003);

if (!process.env.API_TOKEN && process.env.QR_SECRET) {
  process.env.API_TOKEN = process.env.QR_SECRET;
  console.log('[Gateway] API_TOKEN ausente; usando QR_SECRET apenas para a API interna.');
}

const legacyAuthPath = process.env.WWEBJS_AUTH_PATH
  ? process.env.WWEBJS_AUTH_PATH.replace(/\.wwebjs_auth\/?$/, 'baileys_auth')
  : null;

const configuredAuthPath = process.env.BAILEYS_AUTH_PATH || legacyAuthPath || null;
const railwayAuthPath = '/data/baileys_auth';
const legacyPersistentAuthPath = '/var/data/baileys_auth';
const localAuthPath = path.resolve(process.cwd(), '.baileys_auth');
const candidates = [];

if (configuredAuthPath && !configuredAuthPath.startsWith('/tmp/')) candidates.push(configuredAuthPath);
candidates.push(railwayAuthPath, legacyPersistentAuthPath);
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
      if (candidate.startsWith('/tmp/')) console.warn('[Gateway] Sessão em /tmp é efêmera; use um volume persistente.');
      return candidate;
    } catch (error) {
      console.warn(`[Gateway] Auth path indisponível: ${candidate} (${error.code || error.message})`);
    }
  }
  throw new Error('Nenhum diretório gravável disponível para a sessão do WhatsApp.');
}

await ensureBaileysAuthPath();

// 1) Bridge REST/Baileys local.
process.env.PORT = String(bridgePort);
await import('./server-media-v2.js');

// 2) Proxy de áudio: Groq Whisper como principal e URL temporária para fallback.
const { startAudioHandoffProxy } = await import('./audio-handoff-proxy.js');
startAudioHandoffProxy({ listenPort: audioHandoffPort, bridgePort });

// 3) Auto reply orientado a eventos do Baileys, com fallback apenas para o comando de controle.
const { startAutoReplyService } = await import('./auto-reply-service.js');
startAutoReplyService({ bridgePort, audioPort: audioHandoffPort });

// 4) MCP/OAuth: texto e histórico vão direto ao bridge; áudio usa o proxy de transcrição.
const { startMcpGateway } = await import('./mcp-gateway-v5.js');
await startMcpGateway({ publicPort: mcpGatewayPort, bridgePort, audioPort: audioHandoffPort });

// 5) Proxy público de compatibilidade do ChatGPT.
process.env.PORT = String(publicPort);
const { startPublicMcpProxy } = await import('./public-mcp-proxy.js');
startPublicMcpProxy({ publicPort, targetPort: mcpGatewayPort });
