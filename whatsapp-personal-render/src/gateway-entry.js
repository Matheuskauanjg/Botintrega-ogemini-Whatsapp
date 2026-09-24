import 'dotenv/config';

const publicPort = Number(process.env.PORT || 10000);
const internalPort = Number(process.env.BRIDGE_INTERNAL_PORT || 10001);

// Para instalações antigas, QR_SECRET também pode servir como chave da API interna.
// Em produção prefira configurar API_TOKEN e MCP_LOGIN_SECRET separadamente.
if (!process.env.API_TOKEN && process.env.QR_SECRET) {
  process.env.API_TOKEN = process.env.QR_SECRET;
  console.log('[Gateway] API_TOKEN ausente; usando QR_SECRET apenas para a API interna.');
}

// A bridge REST/Baileys fica somente em localhost; a porta pública é do gateway MCP.
process.env.PORT = String(internalPort);
await import('./server.js');

process.env.PORT = String(publicPort);
const { startMcpGateway } = await import('./mcp-gateway.js');
await startMcpGateway({ publicPort, internalPort });
