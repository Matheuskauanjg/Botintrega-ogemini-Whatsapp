const fs = require('fs');
const path = require('path');
const { config } = require('../config/config');

// Importar notificações (pode ser undefined se houver dependência circular)
let notificacoes = null;
try {
  notificacoes = require('./notificacoes');
} catch (error) {
  // Notificações ainda não disponível, será configurado depois
}

// Garantir que os diretórios existam
function garantirDiretorios() {
  try {
    if (!config.PREFS_FILE) {
      console.warn('⚠️ PREFS_FILE não está definido na configuração');
      return;
    }
    
    const dataDir = path.dirname(config.PREFS_FILE);
    
    if (!fs.existsSync(dataDir)) {
      fs.mkdirSync(dataDir, { recursive: true });
    }
  } catch (error) {
    console.error('Erro ao garantir diretórios:', error);
  }
}

// Função para carregar preferências
function carregarPreferencias() {
  try {
    garantirDiretorios();
    
    if (!config.PREFS_FILE) {
      console.warn('⚠️ PREFS_FILE não está definido, usando preferências padrão');
      return getDefaultPreferencias();
    }
    
    if (fs.existsSync(config.PREFS_FILE)) {
      const data = fs.readFileSync(config.PREFS_FILE, 'utf8');
      return JSON.parse(data);
    }
  } catch (error) {
    console.error('Erro ao carregar preferências:', error);
    if (notificacoes && notificacoes.registrarErro) {
      notificacoes.registrarErro('preferencias', 'Erro ao carregar preferências', error);
    }
  }
  return getDefaultPreferencias();
}

// Função auxiliar para obter preferências padrão
function getDefaultPreferencias() {
  return { 
    mensagens_boas: [], 
    dicas: [], 
    ultimo_topico: '',
    feedback: {
      positivo: [],
      negativo: [],
      neutro: []
    },
    metricas: {
      total_mensagens: 0,
      respostas_positivas: 0,
      respostas_negativas: 0,
      ultima_atualizacao: new Date().toISOString()
    }
  };
}

// Função para salvar preferências
function salvarPreferencias(preferencias) {
  try {
    garantirDiretorios();
    fs.writeFileSync(config.PREFS_FILE, JSON.stringify(preferencias, null, 2));
  } catch (error) {
    console.error('Erro ao salvar preferências:', error);
    if (notificacoes) {
      notificacoes.registrarErro('preferencias', 'Erro ao salvar preferências', error);
    }
  }
}

// Adicionar mensagem boa ao histórico
function adicionarMensagemBoa(preferencias, mensagem) {
  if (!preferencias.mensagens_boas.includes(mensagem)) {
    preferencias.mensagens_boas.push(mensagem);
    
    // Limitar a 50 exemplos
    if (preferencias.mensagens_boas.length > 50) {
      preferencias.mensagens_boas = preferencias.mensagens_boas.slice(-50);
    }
    
    // Adicionar também ao feedback positivo estruturado
    adicionarFeedbackEstruturado(preferencias, mensagem, 'positivo');
    
    // Atualizar métricas
    preferencias.metricas.respostas_positivas++;
    preferencias.metricas.total_mensagens++;
    preferencias.metricas.ultima_atualizacao = new Date().toISOString();
    
    salvarPreferencias(preferencias);
    return true;
  }
  return false;
}

// Adicionar dica ao histórico
function adicionarDica(preferencias, dica) {
  if (dica && !preferencias.dicas.includes(dica)) {
    preferencias.dicas.push(dica);
    
    // Limitar a 10 dicas
    if (preferencias.dicas.length > 10) {
      preferencias.dicas = preferencias.dicas.slice(-10);
    }
    
    salvarPreferencias(preferencias);
    return true;
  }
  return false;
}

// Sistema de feedback estruturado
function adicionarFeedbackEstruturado(preferencias, mensagem, tipo) {
  // Garantir que o objeto de feedback existe
  if (!preferencias.feedback) {
    preferencias.feedback = {
      positivo: [],
      negativo: [],
      neutro: []
    };
  }
  
  // Adicionar feedback ao tipo correspondente
  if (!preferencias.feedback[tipo].includes(mensagem)) {
    preferencias.feedback[tipo].push({
      texto: mensagem,
      data: new Date().toISOString(),
      contexto: preferencias.ultimo_topico || 'desconhecido'
    });
    
    // Limitar cada categoria a 50 itens
    if (preferencias.feedback[tipo].length > 50) {
      preferencias.feedback[tipo] = preferencias.feedback[tipo].slice(-50);
    }
    
    return true;
  }
  return false;
}

// Analisar feedback para extrair padrões
function analisarPadroesFeedback(preferencias) {
  const padroes = {
    positivos: {},
    negativos: {}
  };
  
  // Analisar feedback positivo
  if (preferencias.feedback && preferencias.feedback.positivo) {
    preferencias.feedback.positivo.forEach(item => {
      // Extrair palavras-chave
      const palavras = item.texto.toLowerCase().split(/\s+/);
      palavras.forEach(palavra => {
        if (palavra.length > 3) { // Ignorar palavras muito curtas
          padroes.positivos[palavra] = (padroes.positivos[palavra] || 0) + 1;
        }
      });
    });
  }
  
  // Analisar feedback negativo
  if (preferencias.feedback && preferencias.feedback.negativo) {
    preferencias.feedback.negativo.forEach(item => {
      // Extrair palavras-chave
      const palavras = item.texto.toLowerCase().split(/\s+/);
      palavras.forEach(palavra => {
        if (palavra.length > 3) { // Ignorar palavras muito curtas
          padroes.negativos[palavra] = (padroes.negativos[palavra] || 0) + 1;
        }
      });
    });
  }
  
  // Ordenar padrões por frequência
  const padroesPositivos = Object.entries(padroes.positivos)
    .sort((a, b) => b[1] - a[1])
    .slice(0, 10)
    .map(([palavra, contagem]) => ({ palavra, contagem }));
    
  const padroesNegativos = Object.entries(padroes.negativos)
    .sort((a, b) => b[1] - a[1])
    .slice(0, 10)
    .map(([palavra, contagem]) => ({ palavra, contagem }));
  
  return {
    padroesPositivos,
    padroesNegativos
  };
}

// Detectar feedback positivo em uma mensagem
function detectarFeedbackPositivo(texto) {
  if (!texto) return false;
  const t = texto.toLowerCase();
  return /(amei|gostei|perfeito|lindo|maravilhoso|top|kkkk+|kak+|rsrs|❤️|😍|🥰|obrigada|obg)/i.test(t);
}

// Detectar feedback negativo em uma mensagem
function detectarFeedbackNegativo(texto) {
  if (!texto) return false;
  const t = texto.toLowerCase();
  return /(não gostei|ruim|péssimo|horrível|não entendi|confuso|errado|não é isso|tá errado|não foi isso)/i.test(t);
}

// Processar feedback de uma mensagem
function processarFeedback(preferencias, mensagemDela, ultimaMensagemMinha) {
  if (detectarFeedbackPositivo(mensagemDela)) {
    // Adicionar a última mensagem como positiva
    if (ultimaMensagemMinha && ultimaMensagemMinha.texto) {
      adicionarMensagemBoa(preferencias, ultimaMensagemMinha.texto);
      adicionarFeedbackEstruturado(preferencias, ultimaMensagemMinha.texto, 'positivo');
    }
    
    // Extrair dicas baseadas no feedback
    extrairDicasAutomaticas(preferencias, mensagemDela);
    
    return 'positivo';
  } else if (detectarFeedbackNegativo(mensagemDela)) {
    // Registrar feedback negativo
    if (ultimaMensagemMinha && ultimaMensagemMinha.texto) {
      adicionarFeedbackEstruturado(preferencias, ultimaMensagemMinha.texto, 'negativo');
      
      // Atualizar métricas
      preferencias.metricas.respostas_negativas++;
      preferencias.metricas.total_mensagens++;
      preferencias.metricas.ultima_atualizacao = new Date().toISOString();
      
      salvarPreferencias(preferencias);
    }
    
    return 'negativo';
  } else {
    // Feedback neutro
    if (ultimaMensagemMinha && ultimaMensagemMinha.texto) {
      adicionarFeedbackEstruturado(preferencias, ultimaMensagemMinha.texto, 'neutro');
      
      // Atualizar métricas
      preferencias.metricas.total_mensagens++;
      preferencias.metricas.ultima_atualizacao = new Date().toISOString();
      
      salvarPreferencias(preferencias);
    }
    
    return 'neutro';
  }
}

// Extrair dicas automaticamente baseadas no feedback
function extrairDicasAutomaticas(preferencias, texto) {
  if (!texto) return;
  
  const t = texto.toLowerCase();
  const novasDicas = [];
  
  if (/kkkk+|kak+|rsrs/i.test(t)) novasDicas.push('Ela ri com "kkkkk"; humor leve funciona');
  if (/ansiosa|ansiedade|nervosa|tremendo/i.test(t)) novasDicas.push('Acolha ansiedade com calma e apoio');
  if (/dinheiro|pix|ajuda|empresta/i.test(t)) novasDicas.push('Responda ajuda/dinheiro rápido e sem reclamar');
  if (/mk|mortal|kitana/i.test(t)) novasDicas.push('Citar Mortal Kombat/Kitana às vezes agrada');
  
  novasDicas.forEach(dica => {
    adicionarDica(preferencias, dica);
  });
}

// Obter relatório de desempenho
function obterRelatorioDesempenho(preferencias) {
  const metricas = preferencias.metricas || {
    total_mensagens: 0,
    respostas_positivas: 0,
    respostas_negativas: 0
  };
  
  const taxaPositiva = metricas.total_mensagens > 0 
    ? ((metricas.respostas_positivas / metricas.total_mensagens) * 100).toFixed(2) 
    : 0;
  
  const taxaNegativa = metricas.total_mensagens > 0 
    ? ((metricas.respostas_negativas / metricas.total_mensagens) * 100).toFixed(2) 
    : 0;
  
  const padroes = analisarPadroesFeedback(preferencias);
  
  return {
    total_mensagens: metricas.total_mensagens,
    respostas_positivas: metricas.respostas_positivas,
    respostas_negativas: metricas.respostas_negativas,
    taxa_positiva: `${taxaPositiva}%`,
    taxa_negativa: `${taxaNegativa}%`,
    padroes_positivos: padroes.padroesPositivos,
    padroes_negativos: padroes.padroesNegativos,
    ultima_atualizacao: metricas.ultima_atualizacao || new Date().toISOString()
  };
}

// O módulo notificações já foi importado no início do arquivo

// Configurar o módulo de notificações após sua criação
function configurarNotificacoes(moduloNotificacoes) {
  notificacoes = moduloNotificacoes;
}

module.exports = {
  carregarPreferencias,
  salvarPreferencias,
  adicionarMensagemBoa,
  adicionarDica,
  adicionarFeedbackEstruturado,
  processarFeedback,
  detectarFeedbackPositivo,
  detectarFeedbackNegativo,
  obterRelatorioDesempenho,
  configurarNotificacoes
};