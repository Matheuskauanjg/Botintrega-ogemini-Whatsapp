require('dotenv').config();
const readline = require('readline');
const { config, validarConfiguracoes } = require('./config/config');
const whatsapp = require('./services/whatsapp');
const memoria = require('./utils/memoria');
const preferencias = require('./utils/preferencias');
const notificacoes = require('./utils/notificacoes');
const ia = require('./services/ia');
const { tentarReconectar } = require('./utils/reconexao');

console.log('🤖 Iniciando WhatsApp Bot com Gemini AI...');

// Validar configurações antes de iniciar
validarConfiguracoes();

console.log('📱 Conectando ao WhatsApp...');

// Inicializar cliente WhatsApp
const client = whatsapp.inicializarCliente();

// Configurar manipulador de mensagens
whatsapp.configurarManipuladorMensagens(client);

// Inicializar sistema de backup automático
memoria.iniciarBackupAutomatico();

// Função para inicializar com reconexão automática
async function inicializarComReconexao() {
  try {
    console.log('🔄 Inicializando cliente WhatsApp...');
    await client.initialize();
    console.log('✅ Cliente WhatsApp inicializado com sucesso');
  } catch (err) {
    console.error('❌ Erro ao inicializar cliente WhatsApp:', err);
    notificacoes.registrarErro('inicializacao', 'Erro ao inicializar cliente WhatsApp', err);
    
    // Tentar reconectar automaticamente
    console.log('🔄 Iniciando reconexão automática...');
    const novoClient = await tentarReconectar(client);
    if (novoClient) {
      // Atualizar referência do cliente
      Object.assign(client, novoClient);
    }
  }
}

// Inicializar com reconexão automática
inicializarComReconexao();

// Inicializar interface de linha de comando
const rl = readline.createInterface({
  input: process.stdin,
  output: process.stdout
});

// Interface de linha de comando para comandos manuais
console.log('\n📝 Comandos disponíveis:');
console.log('  - "backup": Força um backup da memória de conversas');
console.log('  - "status": Mostra status do sistema');
console.log('  - "erros": Mostra últimos erros registrados');
console.log('  - "relatorio": Mostra relatório de desempenho');
console.log('  - "sair": Encerra o bot');

rl.on('line', async (input) => {
  const comando = input.trim().toLowerCase();
  
  try {
    switch (comando) {
      case 'backup':
        console.log('🔄 Realizando backup manual...');
        memoria.forcarBackup();
        console.log('✅ Backup concluído!');
        break;
        
      case 'status':
        const memoriaData = memoria.carregarMemoria();
        const prefsData = preferencias.carregarPreferencias();
        console.log('\n📊 Status do Sistema:');
        console.log(`  - Mensagens em memória: ${memoriaData.mensagens.length}`);
        console.log(`  - Mensagens boas salvas: ${prefsData.mensagens_boas.length}`);
        console.log(`  - Dicas registradas: ${prefsData.dicas.length}`);
        console.log(`  - Último tópico: "${prefsData.ultimo_topico}"`);
        console.log(`  - Backups disponíveis: ${memoria.listarBackups().length}`);
        
        // Testar API Gemini
        try {
          await ia.testarConexaoGemini();
          console.log('  - API Gemini: ✅ Conectada');
        } catch (error) {
          console.log('  - API Gemini: ❌ Erro de conexão');
        }
        
        // Status do WhatsApp
        console.log(`  - WhatsApp: ${client.info ? '✅ Conectado' : '❌ Desconectado'}`);
        break;
        
      case 'erros':
        const resumoErros = notificacoes.obterResumoErros();
        console.log('\n⚠️ Últimos Erros:');
        if (resumoErros.length === 0) {
          console.log('  Nenhum erro registrado.');
        } else {
          resumoErros.forEach((erro, index) => {
            console.log(`  ${index + 1}. [${erro.categoria}] ${erro.mensagem} (${new Date(erro.timestamp).toLocaleString()})`);
          });
        }
        break;
        
      case 'relatorio':
        const relatorio = preferencias.obterRelatorioDesempenho(preferencias.carregarPreferencias());
        console.log('\n📊 Relatório de Desempenho:');
        console.log(`  - Total de mensagens: ${relatorio.total_mensagens}`);
        console.log(`  - Respostas positivas: ${relatorio.respostas_positivas} (${relatorio.taxa_positiva})`);
        console.log(`  - Respostas negativas: ${relatorio.respostas_negativas} (${relatorio.taxa_negativa})`);
        console.log(`  - Última atualização: ${new Date(relatorio.ultima_atualizacao).toLocaleString()}`);
        break;
        
      case 'sair':
        console.log('👋 Encerrando bot...');
        await client.destroy();
        rl.close();
        process.exit(0);
        break;
        
      default:
        if (comando) {
          console.log('❓ Comando desconhecido. Digite "backup", "status", "erros", "relatorio" ou "sair".');
        }
    }
  } catch (error) {
    console.error('❌ Erro ao processar comando:', error);
    notificacoes.registrarErro('comando', 'Erro ao processar comando da interface', error);
  }
});