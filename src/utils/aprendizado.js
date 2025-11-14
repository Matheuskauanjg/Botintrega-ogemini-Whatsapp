const fs = require('fs');
const path = require('path');
const logger = require('./logger');

// Informações personalizadas genéricas
const informacoesPessoa = {
  nome: 'Nome da Pessoa',
  idade: 0,
  interesses: ['interesse1', 'interesse2', 'interesse3'],
  caracteristicas: {
    altura: 'média',
    olhos: 'castanhos'
  }
};

class SistemaAprendizado {
  constructor() {
    this.dbPath = path.join(__dirname, '../../data/aprendizado.json');
    this.data = this.carregarDados();
  }

  carregarDados() {
    try {
      if (fs.existsSync(this.dbPath)) {
        return JSON.parse(fs.readFileSync(this.dbPath, 'utf8'));
      }
      return {
        feedbacks: [],
        estiloEscrita: {
          vocabulario: {},
          girias: {},
          padroesFrase: []
        },
        tags: {},
        contextos: []
      };
    } catch (error) {
      logger.error('Erro ao carregar dados de aprendizado', error);
      return this.getEstruturaInicial();
    }
  }

  getEstruturaInicial() {
    return {
      feedbacks: [],
      estiloEscrita: {
        vocabulario: {},
        girias: {},
        padroesFrase: []
      },
      tags: {},
      contextos: []
    };
  }

  salvarDados() {
    try {
      fs.writeFileSync(this.dbPath, JSON.stringify(this.data, null, 2));
    } catch (error) {
      logger.error('Erro ao salvar dados de aprendizado', error);
    }
  }

  // Sistema de Feedback
  registrarFeedback(mensagemOriginal, resposta, feedback) {
    this.data.feedbacks.push({
      timestamp: new Date().toISOString(),
      mensagemOriginal,
      resposta,
      feedback,
      pontuacao: this.calcularPontuacaoFeedback(feedback)
    });
    this.salvarDados();
  }

  calcularPontuacaoFeedback(feedback) {
    const feedbackLower = feedback.toLowerCase();
    if (feedbackLower.includes('boa') || feedbackLower.includes('amei') || feedbackLower.includes('perfeita')) {
      return 1;
    }
    if (feedbackLower.includes('ruim') || feedbackLower.includes('não gostei') || feedbackLower.includes('errada')) {
      return -1;
    }
    return 0;
  }

  // Sistema de Tags de Contexto
  classificarContexto(mensagem) {
    const tags = [];
    const mensagemLower = mensagem.toLowerCase();

    // Padrões para identificação de tags
    const padroes = {
      carinho: ['amor', 'beijo', 'saudade', 'abraço', 'te amo'],
      ciumes: ['ciúme', 'com quem', 'onde você', 'quem é'],
      rotina: ['trabalho', 'estudando', 'almoço', 'janta', 'dormindo'],
      planos: ['vamos', 'podemos', 'fim de semana', 'amanhã', 'próximo']
    };

    // Verifica cada padrão
    for (const [tag, palavras] of Object.entries(padroes)) {
      if (palavras.some(palavra => mensagemLower.includes(palavra))) {
        tags.push(tag);
      }
    }

    // Registra a ocorrência das tags
    tags.forEach(tag => {
      this.data.tags[tag] = (this.data.tags[tag] || 0) + 1;
    });

    this.salvarDados();
    return tags;
  }

  // Análise de Estilo de Escrita
  analisarEstilo(mensagem, autor) {
    if (autor !== 'Matheus') {
      return; // Ignorar mensagens enviadas pelo bot
    }

    const palavras = mensagem.split(/\s+/);

    // Atualiza vocabulário
    palavras.forEach(palavra => {
      this.data.estiloEscrita.vocabulario[palavra] = 
        (this.data.estiloEscrita.vocabulario[palavra] || 0) + 1;
    });

    // Identifica possíveis gírias (palavras não comuns)
    const possiveisGirias = palavras.filter(palavra => 
      palavra.length > 3 && 
      !this.isPalavraComum(palavra)
    );

    possiveisGirias.forEach(giria => {
      this.data.estiloEscrita.girias[giria] = 
        (this.data.estiloEscrita.girias[giria] || 0) + 1;
    });

    // Registra padrões de frase
    if (mensagem.length < 100) { // Só guarda frases curtas como padrão
      this.data.estiloEscrita.padroesFrase.push(mensagem);
      // Mantém só os últimos 100 padrões
      if (this.data.estiloEscrita.padroesFrase.length > 100) {
        this.data.estiloEscrita.padroesFrase.shift();
      }
    }

    this.salvarDados();
  }

  isPalavraComum(palavra) {
    const palavrasComuns = ['que', 'com', 'para', 'por', 'mas', 'seu', 'sua'];
    return palavrasComuns.includes(palavra.toLowerCase());
  }

  // Geração de Contexto
  gerarContextoAtual(mensagensRecentes) {
    const contexto = {
      tags: [],
      padroes: [],
      vocabularioPreferido: {},
      giriasComuns: []
    };

    // Analisa mensagens recentes
    mensagensRecentes.forEach(msg => {
      // Adiciona tags
      const tagsMensagem = this.classificarContexto(msg);
      contexto.tags.push(...tagsMensagem);

      // Analisa estilo
      this.analisarEstilo(msg);
    });

    // Pega vocabulário mais usado
    contexto.vocabularioPreferido = this.getTopPalavras(
      this.data.estiloEscrita.vocabulario, 
      10
    );

    // Pega gírias mais comuns
    contexto.giriasComuns = this.getTopPalavras(
      this.data.estiloEscrita.girias,
      5
    );

    return contexto;
  }

  getTopPalavras(dicionario, limite) {
    return Object.entries(dicionario)
      .sort(([,a], [,b]) => b - a)
      .slice(0, limite)
      .map(([palavra]) => palavra);
  }
}

module.exports = new SistemaAprendizado();module.exports.informacoesPessoa = informacoesPessoa;
