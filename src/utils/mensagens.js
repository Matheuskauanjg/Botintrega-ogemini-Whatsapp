// Utilitários para processamento de mensagens

// Sanitiza e encurta resposta: remove emojis, limita a 1 linha curta e varia pequenos detalhes
function sanitizarRespostaCurta(texto) {
  try {
    if (!texto) return '';
    let t = String(texto).trim();
    // limitar emojis a no máximo 2 por mensagem
    const emojis = (t.match(/[\p{Emoji_Presentation}\p{Extended_Pictographic}]/gu) || []).slice(0, 2);
    t = t.replace(/[\p{Emoji_Presentation}\p{Extended_Pictographic}]/gu, '');
    // remove corações e variações ascii
    t = t.replace(/[❤♥💖💘💝💗💓💞💟]+/g, '');
    // normaliza espaços e quebras
    t = t.replace(/\s+/g, ' ').trim();
    // pega somente a primeira linha/sentença curta
    const ponto = t.indexOf('. ');
    const quebra = t.indexOf('\n');
    const corte = [ponto >= 0 ? ponto : t.length, quebra >= 0 ? quebra : t.length].reduce((a,b)=>Math.min(a,b));
    t = t.slice(0, corte);
    // limita a ~120 caracteres
    if (t.length > 120) t = t.slice(0, 120).trim();
    // variações leves: remove duplicações de interjeições
    t = t.replace(/(kkkkk+|haha+|rsrs+)/gi, 'kkkk');
    // reanexar até 2 emojis no final de forma discreta
    if (emojis.length > 0) {
      t = `${t} ${emojis.join('')}`.trim();
    }
    return t;
  } catch {
    return String(texto || '').trim();
  }
}

// Utilitários de envio parcelado
function dividirEmPartes(texto, maxLen) {
  const partes = [];
  let t = String(texto || '');
  while (t.length > maxLen) {
    let corte = t.lastIndexOf('. ', maxLen);
    if (corte === -1) corte = t.lastIndexOf(' ', maxLen);
    if (corte === -1) corte = maxLen;
    partes.push(t.slice(0, corte + 1).trim());
    t = t.slice(corte + 1).trim();
  }
  if (t) partes.push(t);
  return partes;
}

// Função para dormir (esperar)
function dormir(ms) {
  return new Promise(resolve => setTimeout(resolve, ms));
}

// Função para carregar conversas do arquivo de texto
function carregarConversasDoArquivo(arquivoConversas) {
  try {
    const fs = require('fs');
    if (fs.existsSync(arquivoConversas)) {
      const conteudo = fs.readFileSync(arquivoConversas, 'utf8');
      const linhas = conteudo.split('\n');
      const conversas = [];
      
      for (const linha of linhas) {
        if (linha.includes(' - Greed Xd: ') || linha.includes(' - Minha Paixão❤️: ')) {
          const partes = linha.split(' - ');
          if (partes.length >= 3) {
            const autor = partes[2].startsWith('Greed Xd: ') ? 'Matheus' : 'Ela';
            const texto = partes[2].replace(/^(Greed Xd: |Minha Paixão❤️: )/, '');
            conversas.push({ autor, texto });
          }
        }
      }
      
      return conversas.slice(-50); // Últimas 50 mensagens do arquivo
    }
  } catch (error) {
    console.error('Erro ao carregar conversas do arquivo:', error);
  }
  return [];
}

// Heurística: detectar se é pergunta que merece pesquisa web
function ehPerguntaDePesquisa(texto) {
  if (!texto) return false;
  const t = texto.toLowerCase();
  if (/[?？]$/.test(t)) return true;
  const gatilhos = [
    'o que e', 'o que é', 'quem e', 'quem é', 'como funciona', 'como fazer', 'por que', 'porque', 'qual a', 'qual e', 'quando', 'onde', 'sobre', 'procure', 'procura', 'pesquisa', 'pesquise',
    'filme', 'série', 'serie', 'temporada', 'episódio', 'ator', 'atriz', 'diretor', 'resumo', 'livro',
    'matematica', 'matemática', 'formula', 'fórmula', 'equacao', 'equação', 'derivada', 'integral'
  ];
  return gatilhos.some(g => t.includes(g));
}

// Heurística: pedido de aprofundar/"falar mais"/"na wiki"
function ehPedidoDetalhe(texto) {
  if (!texto) return false;
  const t = texto.toLowerCase();
  const gatilhos = [
    'fala mais', 'falar mais', 'mais detalhes', 'detalha', 'explica melhor', 'aprofundar', 'aprofundado', 'wiki', 'wikipedia', 'sobre isso'
  ];
  return gatilhos.some(g => t.includes(g));
}

// Helper: decide se deve responder (responde ~80% das mensagens dela)
function deveResponder(memoria) {
  try {
    // 80% de chance de responder - mantém naturalidade mas é mais responsivo
    return Math.random() < 0.8;
  } catch (_) {
    return true;
  }
}

module.exports = {
  sanitizarRespostaCurta,
  dividirEmPartes,
  dormir,
  carregarConversasDoArquivo,
  ehPerguntaDePesquisa,
  ehPedidoDetalhe,
  deveResponder
};