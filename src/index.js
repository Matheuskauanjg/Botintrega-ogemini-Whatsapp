// Launcher da bridge pessoal do WhatsApp + MCP para o Render.
// Mantido porque o serviço está configurado com Start Command fixo: `node src/index.js`.
import('../whatsapp-personal-render/src/gateway-entry.js').catch(error => {
  console.error('[Launcher] Falha ao iniciar bridge Baileys/MCP:', error);
  process.exit(1);
});
