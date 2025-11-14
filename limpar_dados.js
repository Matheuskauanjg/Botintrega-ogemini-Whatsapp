// Script para limpar memória e preferências antigas
const fs = require('fs');
const path = require('path');

console.log('🧹 Limpando dados antigos...');

// Limpar arquivo de memória
try {
  const memoriaPath = path.join(__dirname, 'data/conversa_memoria.json');
  if (fs.existsSync(memoriaPath)) {
    fs.writeFileSync(memoriaPath, JSON.stringify({ mensagens: [] }, null, 2));
    console.log('✅ Memória de conversa limpa');
  }
} catch (error) {
  console.error('❌ Erro ao limpar memória:', error.message);
}

// Limpar arquivo de preferências
try {
  const preferenciasPath = path.join(__dirname, 'data/preferencias.json');
  if (fs.existsSync(preferenciasPath)) {
    fs.writeFileSync(preferenciasPath, JSON.stringify({ dicas: [], mensagens_boas: [] }, null, 2));
    console.log('✅ Preferências limpas');
  }
} catch (error) {
  console.error('❌ Erro ao limpar preferências:', error.message);
}

// Limpar arquivo de aprendizado
try {
  const aprendizadoPath = path.join(__dirname, 'data/aprendizado.json');
  if (fs.existsSync(aprendizadoPath)) {
    const estruturaLimpa = {
      feedbacks: [],
      estiloEscrita: {
        vocabulario: {},
        girias: {},
        padroesFrase: []
      },
      tags: {},
      contextos: []
    };
    fs.writeFileSync(aprendizadoPath, JSON.stringify(estruturaLimpa, null, 2));
    console.log('✅ Dados de aprendizado limpos');
  }
} catch (error) {
  console.error('❌ Erro ao limpar aprendizado:', error.message);
}

console.log('🎉 Limpeza concluída! O bot agora vai usar apenas as configurações atuais.');