// Script para corrigir o arquivo de preferências com as métricas faltantes
const fs = require('fs');
const path = require('path');

const preferenciasPath = path.join(__dirname, 'data', 'preferencias.json');

try {
  // Ler arquivo atual
  let preferencias = {};
  if (fs.existsSync(preferenciasPath)) {
    const data = fs.readFileSync(preferenciasPath, 'utf8');
    preferencias = JSON.parse(data);
  }

  // Adicionar estrutura de métricas se não existir
  if (!preferencias.metricas) {
    preferencias.metricas = {
      total_mensagens: 0,
      respostas_positivas: 0,
      respostas_negativas: 0,
      ultima_atualizacao: new Date().toISOString()
    };
    console.log('✅ Adicionada estrutura de métricas');
  }

  // Garantir outras propriedades necessárias
  if (!preferencias.feedback) {
    preferencias.feedback = {
      positivo: [],
      negativo: [],
      neutro: []
    };
  }

  if (!preferencias.dicas) {
    preferencias.dicas = [];
  }

  if (!preferencias.mensagens_boas) {
    preferencias.mensagens_boas = [];
  }

  // Salvar arquivo corrigido
  fs.writeFileSync(preferenciasPath, JSON.stringify(preferencias, null, 2));
  
  console.log('✅ Arquivo de preferências corrigido com sucesso!');
  console.log('📊 Estrutura atual:');
  console.log(`   - Total de mensagens: ${preferencias.metricas.total_mensagens}`);
  console.log(`   - Respostas positivas: ${preferencias.metricas.respostas_positivas}`);
  console.log(`   - Respostas negativas: ${preferencias.metricas.respostas_negativas}`);

} catch (error) {
  console.error('❌ Erro ao corrigir preferências:', error);
}