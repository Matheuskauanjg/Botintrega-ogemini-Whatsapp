import 'dotenv/config';

import fs from 'node:fs/promises';
import path from 'node:path';

const publicPort = Number(process.env.PORT || 10000);
const internalPort = Number(process.env.BRIDGE_INTERNAL_PORT || 10001);
const mcpGatewayPort = Number(process.env.MCP_GATEWAY_INTERNAL_PORT || 10002);

// Para instalações antigas, QR_SECRET também pode servir como chave da API interna.
// Em produção prefira configurar API_TOKEN e MCP_LOGIN_SECRET separadamente.
if (!process.env.API_TOKEN && process.env.QR_SECRET) {
  process.env.API_TOKEN = process.env.QR_SECRET;
  console.log('[Gateway] API_TOKEN ausente; usando QR_SECRET apenas para a API interna.');
}

// Render sem Persistent Disk não permite criar /var/data.
// Testamos o caminho configurado e fazemos fallback automático para /tmp,
// que é gravável, porém efêmero (a sessão precisará de novo QR após restart/deploy).
const legacyAuthPath = process.env.WWEBJS_AUTH_PATH
  ? process.env.WWEBJS_AUTH_PATH.replace(/\.wwebjs_auth\/?$/, 'baileys_auth')
  : null;

const requestedAuthPath = process.env.BAILEYS_AUTH_PATH
  || legacyAuthPath
  || path.resolve(process.cwd(), '.baileys_auth');

async function ensureBaileysAuthPath() {
  try {
    await fs.mkdir(requestedAuthPath, { recursive: true });
    process.env.BAILEYS_AUTH_PATH = requestedAuthPath;
    console.log(`[Gateway] Baileys auth path: ${requestedAuthPath}`);
  } catch (error) {
    const fallbackPath = '/tmp/baileys_auth';
    console.warn(`[Gateway] Não foi possível usar ${requestedAuthPath} (${error.code || error.message}). Usando ${fallbackPath}.`);
    await fs.mkdir(fallbackPath, { recursive: true });
    process.env.BAILEYS_AUTH_PATH = fallbackPath;
    console.warn('[Gateway] Aviso: /tmp é efêmero; após restart/deploy pode ser necessário ler o QR novamente.');
  }
}

await ensureBaileysAuthPath();

// 1) Bridge REST/Baileys em localhost:10001.
process.env.PORT = String(internalPort);
await import('./server.js');

// 2) Gateway MCP/OAuth em localhost:10002.
const { startMcpGateway } = await import('./mcp-gateway-v5.js');
await startMcpGateway({ publicPort: mcpGatewayPort, internalPort });

// 3) Proxy público na PORT do Render. Ele adapta o Content-Type enviado pelo scanner do ChatGPT.
process.env.PORT = String(publicPort);
const { startPublicMcpProxy } = await import('./public-mcp-proxy.js');
startPublicMcpProxy({ publicPort, targetPort: mcpGatewayPort });
