const fetch = require('node-fetch');
const { config } = require('../config/config');
const notificacoes = require('../utils/notificacoes');
const { criarPromptPersonalizado } = require('../personalizacao/personalizacao');
const mensagensPersonalizadas = require('../personalizacao/mensagens');

// Função para gerar mensagem de amor com Gemini (agora com personalização)
async function gerarMensagemAmor(preferencias) {
  try {
    // Tentar usar mensagem personalizada primeiro
    const mensagemAleatoria = mensagensPersonalizadas.funcoes.criarMensagem('amor');
    
    // 30% de chance de usar mensagem personalizada direta
    if (Math.random() < 0.3) {
      return mensagemAleatoria;
    }
    
    // Se não, usar IA com prompt personalizado
    const prompt = criarPromptPersonalizado('', 'mensagem de amor aleatória', preferencias);
    
    const response = await fetch(
      `https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key=${config.GEMINI_API_KEY}`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          contents: [{ parts: [{ text: prompt }] }],
          generationConfig: {
            temperature: 0.8,
            topK: 40,
            topP: 0.95,
            maxOutputTokens: config.IA_CONFIG.maxCaracteres,
          }
        })
      }
    );
    
    if (!response.ok) {
      const errorText = await response.text();
      console.error('Erro na API do Gemini:', errorText);
      notificacoes.registrarErro('ia', 'Erro na API do Gemini', new Error(errorText));
      return mensagemAleatoria; // Fallback para mensagem personalizada
    }
    
    const data = await response.json();
    if (data.candidates && data.candidates[0] && data.candidates[0].content) {
      const mensagem = data.candidates[0].content.parts[0].text;
      return mensagem.trim();
    }
    
    return mensagemAleatoria; // Fallback
  } catch (error) {
    console.error('Erro ao gerar mensagem de amor:', error);
    notificacoes.registrarErro('ia', 'Erro ao gerar mensagem de amor', error);
    return mensagensPersonalizadas.funcoes.criarMensagem('amor'); // Fallback seguro
  }
}

// Função para transcrever áudio com Gemini
async function transcreverAudioGemini(media) {
  try {
    // Implementação futura: integração com API de transcrição
    // Por enquanto, retorna mensagem de erro
    console.log('Transcrição de áudio não implementada');
    return "Não consegui transcrever o áudio. Essa funcionalidade será implementada em breve.";
  } catch (error) {
    console.error('Erro ao transcrever áudio:', error);
    notificacoes.registrarErro('ia', 'Erro ao transcrever áudio', error);
    return null;
  }
}

// Função para responder mensagem com Gemini (agora com personalização)
async function responderMensagem(memoria, preferencias, mensagemDela) {
  try {
    // Primeiro, tentar detectar palavras-chave para resposta rápida
    const respostaPalavraChave = mensagensPersonalizadas.funcoes.detectarPalavraChave(mensagemDela);
    if (respostaPalavraChave && Math.random() < 0.4) { // 40% de chance de usar resposta de palavra-chave
      return respostaPalavraChave;
    }
    
    // Obter últimas 5 mensagens para contexto
    const ultimasMensagens = memoria.mensagens.slice(-5);
    const contextoConversa = ultimasMensagens.map(m => `${m.autor}: ${m.texto}`).join('\n');
    
    // Criar prompt personalizado usando a função do Jhennyfer.js
    const prompt = criarPromptPersonalizado(contextoConversa, mensagemDela, preferencias);
    
    const response = await fetch(
      `https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key=${config.GEMINI_API_KEY}`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          contents: [{ parts: [{ text: prompt }] }],
          generationConfig: {
            temperature: 0.8,
            topK: 40,
            topP: 0.95,
            maxOutputTokens: config.IA_CONFIG.maxCaracteres,
          }
        })
      }
    );
    
    if (!response.ok) {
      const errorText = await response.text();
      console.error('Erro na API do Gemini:', errorText);
      notificacoes.registrarErro('ia', 'Erro na API do Gemini', new Error(errorText));
      // Fallback para mensagem personalizada
      return mensagensPersonalizadas.funcoes.criarMensagem('amor');
    }
    
    const data = await response.json();
    if (data.candidates && data.candidates[0] && data.candidates[0].content) {
      const mensagem = data.candidates[0].content.parts[0].text;
      return mensagem.trim();
    }
    
    // Fallback para mensagem personalizada
    return mensagensPersonalizadas.funcoes.criarMensagem('amor');
  } catch (error) {
    console.error('Erro ao responder mensagem:', error);
    notificacoes.registrarErro('ia', 'Erro ao responder mensagem', error);
    // Fallback seguro
    return mensagensPersonalizadas.funcoes.criarMensagem('amor');
  }
}

// Testar conexão com a API do Gemini
async function testarConexaoGemini() {
  try {
    console.log('🧪 Testando API do Gemini...');
    
    const response = await fetch(
      `https://generativelanguage.googleapis.com/v1beta/models?key=${config.GEMINI_API_KEY}`,
      {
        method: "GET",
        headers: { "Content-Type": "application/json" }
      }
    );

    console.log('📊 Status da resposta:', response.status);
    
    if (!response.ok) {
      const errorText = await response.text();
      console.log('❌ Erro:', errorText);
      notificacoes.registrarErro('ia', 'Erro ao testar conexão com Gemini', new Error(errorText));
      return false;
    }

    const data = await response.json();
    console.log('✅ Conexão com API do Gemini estabelecida com sucesso!');
    return true;
    
  } catch (error) {
    console.error('❌ Erro no teste de conexão:', error);
    notificacoes.registrarErro('ia', 'Erro ao testar conexão com Gemini', error);
    return false;
  }
}

module.exports = {
  gerarMensagemAmor,
  transcreverAudioGemini,
  responderMensagem,
  testarConexaoGemini
};