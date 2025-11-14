const { Client, LocalAuth } = require('whatsapp-web.js');
const qrcode = require('qrcode-terminal');
const { config } = require('../config/config');
const memoria = require('../utils/memoria');
const mensagens = require('../utils/mensagens');
const preferencias = require('../utils/preferencias');
const ia = require('./ia');
const notificacoes = require('../utils/notificacoes');

// Configuração do cliente WhatsApp
function criarClienteWhatsApp() {
  console.log('📱 Criando cliente WhatsApp...');
  
  const client = new Client({
    authStrategy: new LocalAuth(),
    puppeteer: {
      headless: true,
      args: [
        '--no-sandbox',
        '--disable-setuid-sandbox',
        '--disable-dev-shm-usage',
        '--disable-accelerated-2d-canvas',
        '--no-first-run',
        '--no-zygote',
        '--disable-gpu'
      ]
    },
    webVersionCache: {
      type: 'remote',
      remotePath: 'https://raw.githubusercontent.com/wppconnect-team/wa-version/main/html/2.2412.54.html',
    }
  });

  // Adicionar evento de erro geral
  client.on('error', (error) => {
    console.error('❌ Erro no cliente WhatsApp:', error);
    notificacoes.registrarErro('whatsapp', 'Erro no cliente WhatsApp', error);
  });

  console.log('✅ Cliente WhatsApp criado com sucesso');
  return client;
}

// Inicializar cliente e configurar eventos
function inicializarCliente() {
  const client = criarClienteWhatsApp();
  
  // Configurar módulos com dependências circulares
  memoria.configurarNotificacoes(notificacoes);
  preferencias.configurarNotificacoes(notificacoes);
  
  // Evento quando QR Code é gerado
  client.on('qr', (qr) => {
    console.log('⚡ Escaneie o QR Code abaixo com seu WhatsApp:');
    qrcode.generate(qr, { small: true });
    console.log('\n📱 Abra o WhatsApp no seu celular e escaneie o código acima');
  });

  // Evento de autenticação bem-sucedida
  client.on('authenticated', (session) => {
    console.log('🔐 Autenticação bem-sucedida!');
  });

  // Evento de falha na autenticação
  client.on('auth_failure', (msg) => {
    console.error('❌ Falha na autenticação:', msg);
    notificacoes.registrarErro('whatsapp', 'Falha na autenticação', new Error(msg));
  });

  // Evento de desconexão
  client.on('disconnected', async (reason) => {
    console.log('📴 WhatsApp desconectado. Razão:', reason);
    notificacoes.registrarErro('whatsapp', 'WhatsApp desconectado', new Error(reason));
    
    // Tentar reconectar automaticamente após desconexão
    console.log('🔄 Iniciando reconexão automática após desconexão...');
    const { tentarReconectar } = require('../utils/reconexao');
    await tentarReconectar(client);
  });

  // Evento de carregamento
  client.on('loading_screen', (percent, message) => {
    console.log(`⏳ Carregando WhatsApp: ${percent}% - ${message}`);
  });

  // Evento de estado da conexão
  client.on('stateChanged', (state) => {
    console.log(`🔄 Estado da conexão mudou para: ${state}`);
  });

  // Evento quando conectado
  client.on('ready', async () => {
    console.log('✅ Conectado ao WhatsApp!');
    console.log('🤖 Bot está ativo e pronto para responder sua namorada!');
    
    try {
      // Testar conexão com a API do Gemini
      await ia.testarConexaoGemini();
      
      // Carrega memória e conversas do arquivo
      console.log('🧠 Carregando memória das conversas...');
      const memoriaData = memoria.carregarMemoria();
      const prefsData = preferencias.carregarPreferencias();
      const arquivoConversas = require('path').join(__dirname, '../../Conversa do WhatsApp com Minha Paixão❤️.txt');
      const conversasArquivo = mensagens.carregarConversasDoArquivo(arquivoConversas);
      
      // Se não tem memória, inicializa com conversas do arquivo
      if (memoriaData.mensagens.length === 0 && conversasArquivo.length > 0) {
        console.log('📖 Carregando conversas do arquivo para base inicial...');
        conversasArquivo.forEach(conv => {
          memoria.adicionarMensagem(memoriaData, conv.autor, conv.texto);
        });
      }
      
      // Mostra últimas 5 mensagens SUAS para contexto
      const ultimasMensagensMinhas = memoria.obterUltimasMensagensMinhas(memoriaData, 5);
      
      if (ultimasMensagensMinhas.length > 0) {
        console.log('📚 Últimas 5 mensagens suas (para contexto):');
        ultimasMensagensMinhas.forEach((msg, index) => {
          console.log(`  ${index + 1}. Você: "${msg.texto}"`);
        });
      } else {
        console.log('📚 Nenhuma mensagem sua anterior na memória');
      }
      
      // Mostra últimas 2 mensagens DELA para contexto
      const ultimasMensagensDelas = memoria.obterUltimasMensagensNaoMinhas(memoriaData, 2);
      
      if (ultimasMensagensDelas.length > 0) {
        console.log('📚 Últimas 2 mensagens dela (para contexto):');
        ultimasMensagensDelas.forEach((msg, index) => {
          console.log(`  ${index + 1}. Ela: "${msg.texto}"`);
        });
      }
      
      // Gera e envia UMA mensagem personalizada com Gemini (reduz mensagens pela metade no início)
      console.log('🤖 Gerando mensagem de amor personalizada...');
      const mensagemAmorRaw = await ia.gerarMensagemAmor(prefsData);
      
      if (mensagemAmorRaw) {
        const mensagemAmor = mensagens.sanitizarRespostaCurta(mensagemAmorRaw);
        await client.sendMessage(config.NAMORADA_NUMBER, mensagemAmor);
        console.log(`❤️ Mensagem de amor enviada: "${mensagemAmor}"`);
        
        // Registrar mensagem na memória
        memoria.adicionarMensagem(memoriaData, 'Matheus', mensagemAmor);
      }
      
      console.log('💕 Aguardando mensagens dela...');
      
      // Enviar notificação de inicialização bem-sucedida
      notificacoes.enviarNotificacaoWhatsApp(client, "Bot iniciado com sucesso! Sistema de backup e notificações ativo.");
      
    } catch (error) {
      console.error('❌ Erro ao enviar mensagem automática:', error);
      notificacoes.registrarErro('inicializacao', 'Erro ao enviar mensagem automática', error);
    }
  });

  // Evento quando desconectado
  client.on('disconnected', (reason) => {
    console.log('❌ Desconectado do WhatsApp:', reason);
    notificacoes.registrarErro('conexao', 'Desconectado do WhatsApp', new Error(reason));
  });

  return client;
}

// Configurar manipulador de mensagens
function configurarManipuladorMensagens(client) {
  // Quando receber mensagem
  client.on('message', async (msg) => {
    console.log(`\n📨 Mensagem recebida de: ${msg.from}`);
    console.log(`📝 Conteúdo: "${msg.body}"`);
    
    // Carrega memória e preferências
    const memoriaData = memoria.carregarMemoria();
    const prefsData = preferencias.carregarPreferencias();
    
    // Verifica se é mensagem sua no grupo de teste
    const chat = await msg.getChat();
    console.log(`\n🔍 Verificando mensagem:`);
    console.log(`- Chat é grupo? ${chat.isGroup}`);
    console.log(`- Nome do chat: "${chat.name}"`);
    console.log(`- Número esperado: ${config.MEU_NUMERO}`);
    console.log(`- Número remetente: ${msg.from}`);
    
    if (chat.isGroup && chat.name.includes(config.GRUPO_TESTE)) {
      console.log('✅ Mensagem é do grupo Teste#Bot');
      
      if (msg.from === config.MEU_NUMERO) {
        console.log('🎯 Mensagem é sua! Salvando como contexto...');
        // Armazena a mensagem na memória como contexto
        memoria.adicionarMensagem(memoriaData, 'Contexto', msg.body);
        console.log('💾 Mensagem salva no contexto');
      } else {
        console.log('❌ Mensagem não é sua, ignorando...');
      }
      return;
    }
    
    console.log(`🔍 Número configurado: ${config.NAMORADA_NUMBER}`);
    console.log(`✅ É da namorada? ${msg.from === config.NAMORADA_NUMBER}`);
    
    // Verifica se a mensagem é da namorada
    if (msg.from === config.NAMORADA_NUMBER) {
      console.log(`\n📩 Mensagem da namorada: "${msg.body}"`);
      
      // Se for áudio, tenta transcrever
      if (msg.hasMedia) {
        try {
          const media = await msg.downloadMedia();
          if (media && media.mimetype && /audio\/ogg; codecs=opus/i.test(media.mimetype)) {
            console.log('🎧 Áudio detectado, iniciando transcrição...');
            const textoTranscrito = await ia.transcreverAudioGemini(media);
            if (textoTranscrito && textoTranscrito.trim()) {
              // substitui o corpo da mensagem pelo texto transcrito para o restante do fluxo
              msg.body = textoTranscrito.trim();
              console.log(`🗒️ Transcrição: "${msg.body}"`);
            } else {
              console.log('⚠️ Transcrição vazia ou falhou.');
            }
          }
        } catch (e) {
          console.error('❌ Erro ao baixar/transcrever áudio:', e.message);
          notificacoes.registrarErro('audio', 'Erro ao baixar/transcrever áudio', e);
        }
      }
      
      // Armazena mensagem dela na memória
      memoria.adicionarMensagem(memoriaData, 'Ela', msg.body);
      
      // Comandos manuais no chat para treinar preferências
      try {
        const textoCru = (msg.body || '').trim();
        const textoLower = textoCru.toLowerCase();
        
        // Comando para adicionar mensagem boa
        if (textoLower === '+boa') {
          const ultimaMinha = memoria.obterUltimasMensagensMinhas(memoriaData, 1)[0];
          if (ultimaMinha && ultimaMinha.texto) {
            preferencias.adicionarMensagemBoa(prefsData, ultimaMinha.texto);
          }
          const ack = 'Anotado, amor. Valeu o toque. 💙';
          memoria.adicionarMensagem(memoriaData, 'Matheus', ack);
          await client.sendMessage(config.NAMORADA_NUMBER, ack);
          return;
        }
        
        // Comando para adicionar dica
        if (textoLower.startsWith('+dica ')) {
          const dica = textoCru.substring(6).trim();
          if (dica) {
            preferencias.adicionarDica(prefsData, dica);
          }
          const ack = 'Dica anotada, mor. Vou lembrar disso. 👍';
          memoria.adicionarMensagem(memoriaData, 'Matheus', ack);
          await client.sendMessage(config.NAMORADA_NUMBER, ack);
          return;
        }
        
        // Comando para ver relatório de desempenho
        if (textoLower === '+relatorio' || textoLower === '+relatório') {
          const relatorio = preferencias.obterRelatorioDesempenho(prefsData);
          const mensagemRelatorio = `📊 Relatório de Desempenho:
- Total de mensagens: ${relatorio.total_mensagens}
- Respostas positivas: ${relatorio.respostas_positivas} (${relatorio.taxa_positiva})
- Respostas negativas: ${relatorio.respostas_negativas} (${relatorio.taxa_negativa})
- Última atualização: ${new Date(relatorio.ultima_atualizacao).toLocaleString()}`;
          
          memoria.adicionarMensagem(memoriaData, 'Matheus', mensagemRelatorio);
          await client.sendMessage(config.NAMORADA_NUMBER, mensagemRelatorio);
          return;
        }
        
        // Comando para forçar backup
        if (textoLower === '+backup') {
          memoria.forcarBackup();
          const ack = '✅ Backup realizado com sucesso!';
          memoria.adicionarMensagem(memoriaData, 'Matheus', ack);
          await client.sendMessage(config.NAMORADA_NUMBER, ack);
          return;
        }
        
      } catch (e) {
        console.error('Erro ao processar comandos de preferências:', e.message);
        notificacoes.registrarErro('comandos', 'Erro ao processar comandos', e);
      }
      
      // Detecta feedback e processa
      try {
        const ultimaMinha = memoria.obterUltimasMensagensMinhas(memoriaData, 1)[0];
        const tipoFeedback = preferencias.processarFeedback(prefsData, msg.body, ultimaMinha);
        console.log(`🔄 Feedback detectado: ${tipoFeedback}`);
      } catch (e) {
        console.error('Erro ao processar feedback:', e.message);
      }
      
      // Decide se deve responder (responde ~80% das mensagens dela)
      if (mensagens.deveResponder(memoriaData)) {
        try {
          console.log('🤖 Gerando resposta...');
          const respostaRaw = await ia.responderMensagem(memoriaData, prefsData, msg.body);
          
          if (respostaRaw) {
            const resposta = mensagens.sanitizarRespostaCurta(respostaRaw);
            await client.sendMessage(config.NAMORADA_NUMBER, resposta);
            console.log(`✅ Resposta enviada: "${resposta}"`);
            
            // Registrar resposta na memória
            memoria.adicionarMensagem(memoriaData, 'Matheus', resposta);
            
            // Atualizar último tópico
            prefsData.ultimo_topico = msg.body;
            preferencias.salvarPreferencias(prefsData);
          }
        } catch (error) {
          console.error('❌ Erro ao gerar/enviar resposta:', error);
          notificacoes.registrarErro('resposta', 'Erro ao gerar/enviar resposta', error);
        }
      } else {
        console.log('🤐 Não respondendo desta vez (alternando respostas)');
      }
    }
  });
}

module.exports = {
  inicializarCliente,
  configurarManipuladorMensagens
};