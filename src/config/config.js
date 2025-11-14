const path = require('path');
require('dotenv').config();

// Importar personalizações
const { getPersonalizacao } = require('../personalizacao/personalizacao');

// Configurações principais - agora usando variáveis de ambiente
const config = {
  // Configurações de API e números (do .env)
  GEMINI_API_KEY: process.env.GEMINI_API_KEY,
  get NAMORADA_NUMBER() {
    // Recarrega do .env toda vez que acessado para evitar cache
    delete require.cache[require.resolve('dotenv')];
    require('dotenv').config();
    return process.env.NAMORADA_NUMBER;
  },
  MEU_NUMBER: process.env.MEU_NUMBER,
  
  // Configurações de backup e memória
  MEMORY_FILE: path.join(__dirname, '../../data/conversa_memoria.json'),
  BACKUP_DIR: path.join(__dirname, '../../data/backups'),
  PREFS_FILE: path.join(__dirname, '../../data/preferencias.json'),
  MAX_MENSAGENS_MEMORIA: 1000,
  BACKUP_INTERVAL: (process.env.BACKUP_INTERVAL_MINUTES || 60) * 60 * 1000, // Converte minutos para ms
  
  // Configurações de notificações
  NOTIFICACOES_ATIVAS: process.env.NOTIFICACOES_ATIVAS === 'true',
  
  // Configurações do WhatsApp
  GRUPO_TESTE: 'Teste#Bot',
  
  // Configurações da IA (usando personalização)
  IA_CONFIG: {
    // Usar configurações do arquivo de personalização
    maxCaracteres: getPersonalizacao().configuracoesResposta.tamanhoMaximo,
    maxEmojis: getPersonalizacao().configuracoesResposta.maxEmojis,
    chanceIronia: getPersonalizacao().configuracoesResposta.chanceIronia,
    frequenciaResposta: getPersonalizacao().configuracoesResposta.frequenciaResposta
  },
  
  // Configurações de segurança e limites
  LIMITES: {
    maxTentativasConexao: 3,
    timeoutResposta: 30000, // 30 segundos
    delayEntreMensagens: 2000 // 2 segundos entre mensagens
  }
};

// Validação de configurações obrigatórias
function validarConfiguracoes() {
  const erros = [];
  
  if (!config.GEMINI_API_KEY) {
    erros.push('GEMINI_API_KEY não configurada no arquivo .env');
  }
  
  if (!config.NAMORADA_NUMBER) {
    erros.push('NAMORADA_NUMBER não configurado no arquivo .env');
  }
  
  if (!config.MEU_NUMBER) {
    erros.push('MEU_NUMBER não configurado no arquivo .env');
  }
  
  if (erros.length > 0) {
    console.error('\n❌ ERRO DE CONFIGURAÇÃO:');
    erros.forEach(erro => console.error(`  - ${erro}`));
    console.error('\n📋 Por favor, configure o arquivo .env com as informações necessárias.');
    console.error('💡 Exemplo de .env:');
    console.error('  GEMINI_API_KEY=sua_chave_aqui');
    console.error('  NAMORADA_NUMBER=558281566233@c.us');
    console.error('  MEU_NUMBER=554199416065@c.us');
    process.exit(1);
  }
  
  console.log('\n✅ Configurações validadas com sucesso!');
  console.log(`📱 Número da namorada: ${config.NAMORADA_NUMBER}`);
  console.log(`🤖 Seu número: ${config.MEU_NUMBER}`);
  console.log(`🔑 API Key configurada: ${config.GEMINI_API_KEY ? 'Sim' : 'Não'}`);
}

// Executar validação ao importar
if (require.main === module) {
  validarConfiguracoes();
}

// Função para recarregar configurações do .env
function recarregarConfiguracoes() {
  delete require.cache[require.resolve('dotenv')];
  require('dotenv').config();
  console.log('🔄 Configurações do .env recarregadas!');
}

module.exports = { config, validarConfiguracoes, recarregarConfiguracoes };