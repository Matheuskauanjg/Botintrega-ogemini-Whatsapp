require('dotenv').config({ path: require('path').resolve(__dirname, '../../.env') });
const fetch = require('node-fetch');
const config = require('../config/config');

/**
 * Testa a conexão com a API do Gemini
 */
async function testGeminiAPI() {
  console.log('🔍 Testando conexão com a API do Gemini...');
  
  try {
    // Verifica se a chave API está configurada
    if (!config.GEMINI_API_KEY) {
      throw new Error('GEMINI_API_KEY não está configurada no arquivo .env');
    }
    
    // Testa a API listando modelos disponíveis
    const url = 'https://generativelanguage.googleapis.com/v1beta/models';
    const response = await fetch(`${url}?key=${config.GEMINI_API_KEY}`);
    
    console.log(`📡 Status da resposta: ${response.status} ${response.statusText}`);
    
    // Verifica se a resposta foi bem-sucedida
    if (!response.ok) {
      const errorText = await response.text();
      throw new Error(`Erro na API: ${response.status} - ${errorText}`);
    }
    
    // Processa a resposta
    const data = await response.json();
    
    // Exibe informações sobre os modelos disponíveis
    console.log('✅ Conexão com a API do Gemini estabelecida com sucesso!');
    console.log(`📊 Modelos disponíveis: ${data.models ? data.models.length : 0}`);
    
    if (data.models && data.models.length > 0) {
      console.log('\n📋 Lista de modelos:');
      data.models.forEach((model, index) => {
        console.log(`  ${index + 1}. ${model.name} - Versão: ${model.version || 'N/A'}`);
      });
    }
    
    return true;
  } catch (error) {
    console.error('❌ Erro ao testar a API do Gemini:', error.message);
    return false;
  }
}

// Executa o teste se este arquivo for executado diretamente
if (require.main === module) {
  testGeminiAPI()
    .then(success => {
      if (!success) {
        process.exit(1);
      }
    })
    .catch(error => {
      console.error('❌ Erro não tratado:', error);
      process.exit(1);
    });
}

module.exports = { testGeminiAPI };