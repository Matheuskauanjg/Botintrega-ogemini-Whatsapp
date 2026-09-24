require('dotenv').config();
const readline = require('readline');
const express = require('express');
const QRCode = require('qrcode');
const { config, validarConfiguracoes } = require('./config/config');
const whatsapp = require('./services/whatsapp');
const memoria = require('./utils/memoria');
const preferencias = require('./utils/preferencias');
const notificacoes = require('./utils/notificacoes');
const ia = require('./services/ia');
const { tentarReconectar } = require('./utils/reconexao');

console.log('🤖 Iniciando WhatsApp Bot com Gemini AI...');

validarConfiguracoes();
console.log('📱 Conectando ao WhatsApp...');

const client = whatsapp.inicializarCliente();
whatsapp.configurarManipuladorMensagens(client);
memoria.iniciarBackupAutomatico();

let latestQrDataUrl = null;
let whatsappState = 'starting';
let lastError = null;

client.on('qr', async (qr) => {
  try {
    latestQrDataUrl = await QRCode.toDataURL(qr, {
      width: 460,
      margin: 2,
      errorCorrectionLevel: 'M'
    });
    whatsappState = 'waiting_for_qr_scan';
    lastError = null;
    console.log('🌐 QR gráfico disponível em /qr');
  } catch (error) {
    lastError = error.message;
    console.error('❌ Erro ao gerar QR gráfico:', error);
  }
});

client.on('authenticated', () => {
  whatsappState = 'authenticated';
  latestQrDataUrl = null;
});

client.on('ready', () => {
  whatsappState = 'ready';
  latestQrDataUrl = null;
  lastError = null;
});

client.on('auth_failure', (message) => {
  whatsappState = 'auth_failure';
  lastError = String(message || 'Falha na autenticação');
});

client.on('disconnected', (reason) => {
  whatsappState = 'disconnected';
  lastError = String(reason || 'Desconectado');
});

const app = express();
const PORT = Number(process.env.PORT || 10000);

app.get('/', (_req, res) => {
  res.type('html').send(`<!doctype html>
<html lang="pt-BR">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>WhatsApp Bot</title>
</head>
<body style="font-family:Arial,sans-serif;max-width:720px;margin:40px auto;padding:0 20px">
<h1>WhatsApp Bot</h1>
<p>Serviço ativo.</p>
<p><a href="/qr">Abrir QR Code do WhatsApp</a></p>
<p>Status: <strong>${whatsappState}</strong></p>
</body>
</html>`);
});

app.get('/health', (_req, res) => {
  res.json({
    ok: true,
    whatsappState,
    ready: whatsappState === 'ready',
    hasQr: Boolean(latestQrDataUrl),
    lastError
  });
});

app.get('/qr', (_req, res) => {
  let content;

  if (latestQrDataUrl) {
    content = `
      <div class="status waiting">Aguardando leitura</div>
      <img src="${latestQrDataUrl}" alt="QR Code do WhatsApp" class="qr">
      <p class="help">No celular: WhatsApp → Dispositivos conectados → Conectar dispositivo.</p>
    `;
  } else if (whatsappState === 'ready' || whatsappState === 'authenticated') {
    content = `
      <div class="check">✓</div>
      <h2>WhatsApp conectado</h2>
      <p class="help">A sessão foi autenticada com sucesso.</p>
    `;
  } else {
    content = `
      <div class="spinner"></div>
      <h2>Gerando QR Code...</h2>
      <p class="help">A página atualiza automaticamente. Se acabou de iniciar o serviço, aguarde o WhatsApp Web gerar o código.</p>
    `;
  }

  res.type('html').send(`<!doctype html>
<html lang="pt-BR">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="4">
<title>Conectar WhatsApp</title>
<style>
*{box-sizing:border-box}
body{margin:0;min-height:100vh;display:grid;place-items:center;background:#eef3f1;font-family:Arial,sans-serif;color:#13251f;padding:24px}
.card{width:min(560px,100%);background:#fff;border-radius:24px;padding:32px;text-align:center;box-shadow:0 14px 45px rgba(0,0,0,.10)}
h1{margin-top:0}.qr{display:block;width:min(460px,100%);height:auto;margin:20px auto;background:#fff;border-radius:18px;border:1px solid #dce5e1;padding:12px}
.status{display:inline-block;padding:8px 14px;border-radius:999px;font-weight:700}.waiting{background:#fff3cd;color:#765800}.help{color:#536760;line-height:1.5}.state{margin-top:20px;font-size:14px;color:#6b7d76}.check{width:84px;height:84px;border-radius:50%;display:grid;place-items:center;margin:20px auto;background:#dff7e8;color:#13763d;font-size:48px;font-weight:bold}.spinner{width:52px;height:52px;border:6px solid #d8e1dd;border-top-color:#25d366;border-radius:50%;margin:24px auto;animation:spin 1s linear infinite}@keyframes spin{to{transform:rotate(360deg)}}
</style>
</head>
<body>
<main class="card">
<h1>Conectar WhatsApp</h1>
${content}
<div class="state">Estado: <strong>${whatsappState}</strong>${lastError ? `<br>Erro: ${String(lastError).replace(/</g, '&lt;')}` : ''}</div>
</main>
</body>
</html>`);
});

app.listen(PORT, '0.0.0.0', () => {
  console.log(`🌐 Servidor HTTP ativo na porta ${PORT}`);
  console.log(`🌐 Abra /qr para visualizar o QR Code`);
});

async function inicializarComReconexao() {
  try {
    console.log('🔄 Inicializando cliente WhatsApp...');
    await client.initialize();
    console.log('✅ Cliente WhatsApp inicializado com sucesso');
  } catch (err) {
    whatsappState = 'initialization_error';
    lastError = err.message;
    console.error('❌ Erro ao inicializar cliente WhatsApp:', err);
    notificacoes.registrarErro('inicializacao', 'Erro ao inicializar cliente WhatsApp', err);

    console.log('🔄 Iniciando reconexão automática...');
    const novoClient = await tentarReconectar(client);
    if (novoClient) {
      Object.assign(client, novoClient);
    }
  }
}

inicializarComReconexao();

const rl = readline.createInterface({
  input: process.stdin,
  output: process.stdout
});

console.log('\n📝 Comandos disponíveis:');
console.log('  - "backup": Força um backup da memória de conversas');
console.log('  - "status": Mostra status do sistema');
console.log('  - "erros": Mostra últimos erros registrados');
console.log('  - "relatorio": Mostra relatório de desempenho');
console.log('  - "sair": Encerra o bot');

rl.on('line', async (input) => {
  const comando = input.trim().toLowerCase();

  try {
    switch (comando) {
      case 'backup':
        console.log('🔄 Realizando backup manual...');
        memoria.forcarBackup();
        console.log('✅ Backup concluído!');
        break;

      case 'status': {
        const memoriaData = memoria.carregarMemoria();
        const prefsData = preferencias.carregarPreferencias();
        console.log('\n📊 Status do Sistema:');
        console.log(`  - Mensagens em memória: ${memoriaData.mensagens.length}`);
        console.log(`  - Mensagens boas salvas: ${prefsData.mensagens_boas.length}`);
        console.log(`  - Dicas registradas: ${prefsData.dicas.length}`);
        console.log(`  - Último tópico: "${prefsData.ultimo_topico}"`);
        console.log(`  - Backups disponíveis: ${memoria.listarBackups().length}`);

        try {
          await ia.testarConexaoGemini();
          console.log('  - API Gemini: ✅ Conectada');
        } catch (error) {
          console.log('  - API Gemini: ❌ Erro de conexão');
        }

        console.log(`  - WhatsApp: ${client.info ? '✅ Conectado' : '❌ Desconectado'}`);
        break;
      }

      case 'erros': {
        const resumoErros = notificacoes.obterResumoErros();
        console.log('\n⚠️ Últimos Erros:');
        if (resumoErros.length === 0) {
          console.log('  Nenhum erro registrado.');
        } else {
          resumoErros.forEach((erro, index) => {
            console.log(`  ${index + 1}. [${erro.categoria}] ${erro.mensagem} (${new Date(erro.timestamp).toLocaleString()})`);
          });
        }
        break;
      }

      case 'relatorio': {
        const relatorio = preferencias.obterRelatorioDesempenho(preferencias.carregarPreferencias());
        console.log('\n📊 Relatório de Desempenho:');
        console.log(`  - Total de mensagens: ${relatorio.total_mensagens}`);
        console.log(`  - Respostas positivas: ${relatorio.respostas_positivas} (${relatorio.taxa_positiva})`);
        console.log(`  - Respostas negativas: ${relatorio.respostas_negativas} (${relatorio.taxa_negativa})`);
        console.log(`  - Última atualização: ${new Date(relatorio.ultima_atualizacao).toLocaleString()}`);
        break;
      }

      case 'sair':
        console.log('👋 Encerrando bot...');
        await client.destroy();
        rl.close();
        process.exit(0);
        break;

      default:
        if (comando) {
          console.log('❓ Comando desconhecido. Digite "backup", "status", "erros", "relatorio" ou "sair".');
        }
    }
  } catch (error) {
    console.error('❌ Erro ao processar comando:', error);
    notificacoes.registrarErro('comando', 'Erro ao processar comando da interface', error);
  }
});
