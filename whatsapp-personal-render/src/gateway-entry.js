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
process.env.PORT = String(bridgePort);
await import('./server-media-v2.js');

// 2) Proxy interno de áudio: Groq Whisper como principal e URL temporária
// para WhisperAI apenas quando a transcrição principal falhar.
const { startAudioHandoffProxy } = await import('./audio-handoff-proxy.js');
startAudioHandoffProxy({ listenPort: audioHandoffPort, bridgePort });

// 3) Respostas automáticas controladas pelo número definido em
// AUTO_REPLY_CONTROL_NUMBER. Começa OFF na primeira execução e persiste o estado.
const { startAutoReplyService } = await import('./auto-reply-service.js');
startAutoReplyService({ bridgePort, audioPort: audioHandoffPort });

// 4) Gateway MCP/OAuth usa o proxy de áudio como API interna.
const { startMcpGateway } = await import('./mcp-gateway-v5.js');
await startMcpGateway({ publicPort: mcpGatewayPort, internalPort: audioHandoffPort });

// 5) Proxy público na PORT do Render.
process.env.PORT = String(publicPort);
const { startPublicMcpProxy } = await import('./public-mcp-proxy.js');
startPublicMcpProxy({ publicPort, targetPort: mcpGatewayPort });
