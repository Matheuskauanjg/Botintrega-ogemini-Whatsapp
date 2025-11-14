const fs = require('fs');
const path = require('path');
const { config } = require('../config/config');

// Garantir que os diretórios existam
function garantirDiretorios() {
  const dataDir = path.dirname(config.MEMORY_FILE);
  const backupDir = config.BACKUP_DIR;
  
  if (!fs.existsSync(dataDir)) {
    fs.mkdirSync(dataDir, { recursive: true });
  }
  
  if (!fs.existsSync(backupDir)) {
    fs.mkdirSync(backupDir, { recursive: true });
  }
}

// Função para carregar memória
function carregarMemoria() {
  try {
    garantirDiretorios();
    if (fs.existsSync(config.MEMORY_FILE)) {
      const data = fs.readFileSync(config.MEMORY_FILE, 'utf8');
      return JSON.parse(data);
    }
  } catch (error) {
    console.error('Erro ao carregar memória:', error);
    notificacoes.registrarErro('memoria', 'Erro ao carregar memória', error);
  }
  return { mensagens: [] };
}

// Função para salvar memória
function salvarMemoria(memoria) {
  try {
    garantirDiretorios();
    fs.writeFileSync(config.MEMORY_FILE, JSON.stringify(memoria, null, 2));
    
    // Verificar se é hora de fazer backup
    verificarEFazerBackup();
  } catch (error) {
    console.error('Erro ao salvar memória:', error);
    notificacoes.registrarErro('memoria', 'Erro ao salvar memória', error);
  }
}

// Sistema de backup
let ultimoBackup = 0;

function verificarEFazerBackup() {
  const agora = Date.now();
  if (agora - ultimoBackup > config.BACKUP_INTERVAL) {
    fazerBackup();
    ultimoBackup = agora;
  }
}

function fazerBackup() {
  try {
    garantirDiretorios();
    
    // Criar nome de arquivo com timestamp
    const timestamp = new Date().toISOString().replace(/[:.]/g, '-');
    const backupFile = path.join(config.BACKUP_DIR, `conversa_backup_${timestamp}.json`);
    
    // Copiar arquivo atual para backup
    if (fs.existsSync(config.MEMORY_FILE)) {
      fs.copyFileSync(config.MEMORY_FILE, backupFile);
      console.log(`✅ Backup criado: ${backupFile}`);
      
      // Limpar backups antigos (manter apenas os 10 mais recentes)
      limparBackupsAntigos();
    }
  } catch (error) {
    console.error('Erro ao criar backup:', error);
    notificacoes.registrarErro('backup', 'Erro ao criar backup', error);
  }
}

function limparBackupsAntigos() {
  try {
    const arquivos = fs.readdirSync(config.BACKUP_DIR)
      .filter(file => file.startsWith('conversa_backup_'))
      .map(file => ({
        nome: file,
        caminho: path.join(config.BACKUP_DIR, file),
        data: fs.statSync(path.join(config.BACKUP_DIR, file)).mtime.getTime()
      }))
      .sort((a, b) => b.data - a.data); // Ordenar do mais recente para o mais antigo
    
    // Manter apenas os 10 backups mais recentes
    if (arquivos.length > 10) {
      arquivos.slice(10).forEach(arquivo => {
        fs.unlinkSync(arquivo.caminho);
        console.log(`🗑️ Backup antigo removido: ${arquivo.nome}`);
      });
    }
  } catch (error) {
    console.error('Erro ao limpar backups antigos:', error);
  }
}

// Função para adicionar mensagem à memória
function adicionarMensagem(memoria, autor, texto, timestamp) {
  const mensagem = {
    autor: autor,
    texto: texto,
    timestamp: timestamp || new Date().toISOString()
  };
  
  memoria.mensagens.push(mensagem);
  
  // Manter apenas as últimas X mensagens para não sobrecarregar
  if (memoria.mensagens.length > config.MAX_MENSAGENS_MEMORIA) {
    memoria.mensagens = memoria.mensagens.slice(-config.MAX_MENSAGENS_MEMORIA);
  }
  
  salvarMemoria(memoria);
  return memoria;
}

// Função para obter últimas mensagens
function obterUltimasMensagens(memoria, quantidade = 5) {
  return memoria.mensagens.slice(-quantidade);
}

// Função para obter últimas mensagens que NÃO foram enviadas por ele
function obterUltimasMensagensNaoMinhas(memoria, quantidade = 5) {
  const mensagensNaoMinhas = memoria.mensagens.filter(msg => msg.autor !== 'Matheus');
  return mensagensNaoMinhas.slice(-quantidade);
}

// Função para obter últimas mensagens SUAS
function obterUltimasMensagensMinhas(memoria, quantidade = 5) {
  const mensagensMinhas = memoria.mensagens.filter(msg => msg.autor === 'Matheus');
  return mensagensMinhas.slice(-quantidade);
}

// Forçar backup manual
function forcarBackup() {
  fazerBackup();
  return true;
}

// Restaurar de um backup específico
function restaurarDeBackup(nomeArquivo) {
  try {
    const caminhoBackup = path.join(config.BACKUP_DIR, nomeArquivo);
    if (fs.existsSync(caminhoBackup)) {
      const dados = fs.readFileSync(caminhoBackup, 'utf8');
      const memoria = JSON.parse(dados);
      salvarMemoria(memoria);
      console.log(`✅ Restaurado do backup: ${nomeArquivo}`);
      return true;
    } else {
      console.error(`Arquivo de backup não encontrado: ${nomeArquivo}`);
      return false;
    }
  } catch (error) {
    console.error('Erro ao restaurar do backup:', error);
    notificacoes.registrarErro('backup', 'Erro ao restaurar do backup', error);
    return false;
  }
}

// Listar backups disponíveis
function listarBackups() {
  try {
    garantirDiretorios();
    return fs.readdirSync(config.BACKUP_DIR)
      .filter(file => file.startsWith('conversa_backup_'))
      .map(file => ({
        nome: file,
        data: fs.statSync(path.join(config.BACKUP_DIR, file)).mtime
      }))
      .sort((a, b) => b.data - a.data); // Ordenar do mais recente para o mais antigo
  } catch (error) {
    console.error('Erro ao listar backups:', error);
    return [];
  }
}

// Importar módulo de notificações (será definido depois)
let notificacoes = { registrarErro: () => {} };

// Configurar o módulo de notificações após sua criação
function configurarNotificacoes(moduloNotificacoes) {
  notificacoes = moduloNotificacoes;
}

// Iniciar sistema de backup automático
function iniciarBackupAutomatico() {
  console.log('🔄 Iniciando sistema de backup automático...');
  // Fazer backup inicial
  fazerBackup();
  
  // Configurar intervalo para backups periódicos
  setInterval(() => {
    console.log('🔄 Executando backup automático...');
    fazerBackup();
  }, config.BACKUP_INTERVAL);
  
  console.log(`✅ Sistema de backup configurado (intervalo: ${config.BACKUP_INTERVAL / 60000} minutos)`);
}

module.exports = {
  carregarMemoria,
  salvarMemoria,
  adicionarMensagem,
  obterUltimasMensagens,
  obterUltimasMensagensNaoMinhas,
  obterUltimasMensagensMinhas,
  forcarBackup,
  restaurarDeBackup,
  iniciarBackupAutomatico,
  listarBackups,
  configurarNotificacoes
};