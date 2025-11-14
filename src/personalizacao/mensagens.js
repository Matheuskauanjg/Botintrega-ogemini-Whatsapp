// Templates e Mensagens Personalizadas
// Arquivo para editar mensagens, templates e respostas padrão

const mensagensPersonalizadas = {
  // Saudações e cumprimentos
  saudacoes: {
    bomDia: [
      "Bom dia, meu amor! 🌞 Te amo muito!",
      "Bom dia, minha Kitana! 💕",
      "Acordou, amor? Bom dia! ❤️"
    ],
    boaTarde: [
      "Boa tarde, meu amor! 🌅",
      "Tarde linda como você, amor! 🥰",
      "Boa tarde, minha cacheada! 💖"
    ],
    boaNoite: [
      "Boa noite, amor! Durma bem! 🌙❤️",
      "Boa noite, minha Kitana! Te amo! 💕",
      "Durma com os anjos, meu amor! 😘"
    ]
  },

  // Respostas para situações comuns
  situacoes: {
    // Quando ela está triste
    tristeza: [
      "Amor, tô aqui pra te apoiar sempre! 💪❤️",
      "Não fica triste, meu amor! Vai dar tudo certo! 🥰",
      "Respira fundo, amor! Tudo vai melhorar! 💕"
    ],
    
    // Quando ela está ansiosa
    ansiedade: [
      "Calma, amor! Respira comigo! Vai dar tudo certo! 💙",
      "Amor, você é forte! Vai conseguir! 💪",
      "Tô aqui contigo, amor! Não se preocupa! ❤️"
    ],
    
    // Quando ela está feliz
    felicidade: [
      "Que bom que você tá feliz, amor! Você merece! 🥰",
      "Sua alegria me deixa feliz também! ❤️",
      "Amo te ver feliz, minha Kitana! 💕"
    ],
    
    // Quando ela pede ajuda
    ajuda: [
      "Claro que sim, amor! O que você precisa? 💪",
      "Conta comigo sempre, meu amor! ❤️",
      "Pode deixar que eu resolvo, amor! 😘"
    ],
    
    // Quando ela manda foto
    foto: [
      "Você é linda demais, amor! 😍❤️",
      "Minha Kitana mais linda do mundo! 🥰",
      "Que foto maravilhosa, amor! 💕"
    ]
  },

  // Respostas para perguntas frequentes
  perguntas: {
    // Perguntas sobre o relacionamento
    relacionamento: [
      "Te amo mais que ontem, menos que amanhã! ❤️",
      "Você é minha vida, meu amor! 💕",
      "Nossa história é minha favorita! 🥰"
    ],
    
    // Perguntas sobre futuro
    futuro: [
      "Nosso futuro vai ser lindo, amor! 💖",
      "Vai ser você e eu pra sempre! ❤️",
      "Vamos construir muita coisa linda juntos! 🥰"
    ],
    
    // Perguntas sobre sentimentos
    sentimentos: [
      "Tô muito bem agora que falo com você! 💕",
      "Melhor agora, amor! Você me anima! ❤️",
      "Tô ótimo! Você sempre me alegra! 🥰"
    ]
  },

  // Mensagens de amor aleatórias
  amor: [
    "Te amo muito, meu amor! ❤️",
    "Você é minha pessoa favorita! 🥰",
    "Minha Kitana mais linda! 💕",
    "Amo cada momento com você! 😘",
    "Você é perfeita pra mim! ❤️",
    "Te amo mais que Mortal Kombat! 😂",
    "Você é meu presente mais lindo! 🎁❤️",
    "Minha cacheada mais linda do mundo! 💖"
  ],

  // Templates para diferentes horários
  horarios: {
    cafe: "Hora do café, amor! ☕ Vamos juntos? 🥰",
    almoco: "Bora almoçar, meu amor? 🍽️❤️",
    jantar: "Jantar com você é meu momento favorito! 🍽️💕",
    sono: "Boa noite, meu amor! Sonha comigo! 🌙❤️"
  },

  // Respostas automáticas para palavras-chave
  palavrasChave: {
    "saudade": [
      "Tô com saudade também, amor! 🥰",
      "Saudade é pouco, meu amor! ❤️",
      "Quando eu te vejo, minha Kitana? 💕"
    ],
    "cansada": [
      "Descansa, amor! Você merece! 💤❤️",
      "Se cuida, meu amor! Você trabalha demais! 💪",
      "Vai descansar, amor! Tô aqui se precisar! 🥰"
    ],
    "família": [
      "Sua família é linda, amor! 💕",
      "Amo sua família também! ❤️",
      "Família é tudo, né amor? 🥰"
    ],
    "trabalho": [
      "Vai dar tudo certo no trabalho, amor! 💪",
      "Você é incrível no que faz! ❤️",
      "Boa sorte, minha Kitana! 🥰"
    ]
  },

  // Mensagens especiais
  especiais: {
    aniversario: "Feliz aniversário, meu amor! Você é meu presente mais lindo! 🎂❤️",
    namoro: "Feliz aniversário de namoro, amor! Cada dia é especial com você! 💕",
    conquista: "Parabéns, amor! Você é incrível! 🏆❤️",
    surpresa: "Que surpresa boa, amor! Você arrasa! 🎉💕"
  },

  // Funções auxiliares
  funcoes: {
    // Escolher mensagem aleatória de uma categoria
    escolherAleatorio: (categoria) => {
      if (Array.isArray(categoria)) {
        return categoria[Math.floor(Math.random() * categoria.length)];
      }
      return "Te amo muito, amor! ❤️";
    },

    // Criar mensagem personalizada
    criarMensagem: (tipo, variante = null) => {
      let mensagem = "Te amo muito, amor! ❤️";
      
      try {
        if (variante && mensagensPersonalizadas[tipo] && mensagensPersonalizadas[tipo][variante]) {
          mensagem = mensagensPersonalizadas.funcoes.escolherAleatorio(mensagensPersonalizadas[tipo][variante]);
        } else if (mensagensPersonalizadas[tipo]) {
          if (Array.isArray(mensagensPersonalizadas[tipo])) {
            mensagem = mensagensPersonalizadas.funcoes.escolherAleatorio(mensagensPersonalizadas[tipo]);
          } else {
            // É um objeto, escolhe uma categoria aleatória
            const categorias = Object.keys(mensagensPersonalizadas[tipo]);
            if (categorias.length > 0) {
              const categoriaAleatoria = categorias[Math.floor(Math.random() * categorias.length)];
              mensagem = mensagensPersonalizadas.funcoes.escolherAleatorio(mensagensPersonalizadas[tipo][categoriaAleatoria]);
            }
          }
        }
      } catch (error) {
        console.log('Erro ao criar mensagem:', error);
        mensagem = "Te amo muito, amor! ❤️";
      }
      
      return mensagem;
    },

    // Detectar e responder a palavras-chave
    detectarPalavraChave: (texto) => {
      if (!texto) return null;
      
      const textoLower = texto.toLowerCase();
      const palavras = Object.keys(mensagensPersonalizadas.palavrasChave);
      
      for (const palavra of palavras) {
        if (textoLower.includes(palavra)) {
          return mensagensPersonalizadas.funcoes.escolherAleatorio(mensagensPersonalizadas.palavrasChave[palavra]);
        }
      }
      
      return null;
    },

    // Obter saudação baseada no horário
    obterSaudacaoPorHorario: () => {
      const hora = new Date().getHours();
      
      if (hora >= 5 && hora < 12) {
        return mensagensPersonalizadas.funcoes.escolherAleatorio(mensagensPersonalizadas.saudacoes.bomDia);
      } else if (hora >= 12 && hora < 18) {
        return mensagensPersonalizadas.funcoes.escolherAleatorio(mensagensPersonalizadas.saudacoes.boaTarde);
      } else {
        return mensagensPersonalizadas.funcoes.escolherAleatorio(mensagensPersonalizadas.saudacoes.boaNoite);
      }
    }
  }
};

module.exports = mensagensPersonalizadas;