// Função para gerenciar reconexão automática
const RECONNECT_DELAY = 30000; // 30 segundos
let reconectarTentativas = 0;
let isReconnecting = false;

async function tentarReconectar(client) {
  if (isReconnecting) return;
  
  isReconnecting = true;
  reconectarTentativas++;
  
  console.log(`🔄 Tentando reconectar... (Tentativa ${reconectarTentativas})`);
  
  try {
    // Destruir cliente antigo se existir
    if (client && client.destroy) {
      await client.destroy();
    }
    
    // Criar novo cliente
    const novoClient = whatsapp.criarClienteWhatsApp();
    whatsapp.configurarManipuladorMensagens(novoClient);
    
    // Tentar inicializar
    await novoClient.initialize();
    
    console.log('✅ Reconexão bem-sucedida!');
    reconectarTentativas = 0;
    isReconnecting = false;
    
    return novoClient;
    
  } catch (error) {
    console.error(`❌ Falha na reconexão (tentativa ${reconectarTentativas}):`, error.message);
    
    // Agendar próxima tentativa
    setTimeout(() => {
      isReconnecting = false;
      tentarReconectar(client);
    }, RECONNECT_DELAY);
    
    return null;
  }
}

module.exports = {
  tentarReconectar
};