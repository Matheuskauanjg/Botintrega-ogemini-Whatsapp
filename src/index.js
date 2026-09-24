// Launcher da bridge pessoal do WhatsApp para o Render.
// O serviço está configurado com Start Command fixo: `node src/index.js`.
// A bridge Baileys é ESM, então carregamos via import dinâmico.
import('../whatsapp-personal-render/src/server.js').catch(error => {
  console.error('[Launcher] Falha ao iniciar bridge Baileys:', error);
  process.exit(1);
});
