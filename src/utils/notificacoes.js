const fs = require('fs');
const path = require('path');
const config = require('../config/config');

// Diretório para logs de erros
const LOG_DIR = path.join(__dirname, '../../logs');
const ERROR_LOG = path.join(LOG_DIR, 'erros.log');

// Garantir que o diretório de logs exista
function garantirDiretorioLogs() {
  if (!fs.existsSync(LOG_DIR)) {
    fs.mkdirSync(LOG_DIR, { recursive: true });
  }
}

// Registrar erro no log
function registrarErro(categoria, mensagem, erro) {
  try {
    garantirDiretorioLogs();
    
    const timestamp = new Date().toISOString();
    const detalhes = erro ? (erro.stack || erro.message || JSON.stringify(erro)) : 'Sem detalhes';
    const logEntry = `[${timestamp}] [${categoria.toUpperCase()}] ${mensagem}: ${detalhes}\n`;
    
    fs.appendFileSync(ERROR_LOG, logEntry);
    
    // Se as notificações estiverem ativas, exibir no console
    if (config.NOTIFICACOES_ATIVAS) {
      console.error(`❌ [${categoria.toUpperCase()}] ${mensagem}`);
    }
    
    // Verificar se o arquivo de log está muito grande
    verificarTamanhoLog();
    
    return true;
  } catch (e) {
    console.error('Erro ao registrar erro no log:', e);
    return false;
  }
}

// Verificar tamanho do arquivo de log e rotacionar se necessário
function verificarTamanhoLog() {
  try {
    if (fs.existsSync(ERROR_LOG)) {
      const stats = fs.statSync(ERROR_LOG);
      const fileSizeInMB = stats.size / (1024 * 1024);
      
      // Se o arquivo for maior que 5MB, rotacionar
      if (fileSizeInMB > 5) {
        rotacionarLog();
      }
    }
  } catch (e) {
    console.error('Erro ao verificar tamanho do log:', e);
  }
}

// Rotacionar arquivo de log
function rotacionarLog() {
  try {
    const timestamp = new Date().toISOString().replace(/[:.]/g, '-');
    const newLogFile = path.join(LOG_DIR, `erros_${timestamp}.log`);
    
    fs.renameSync(ERROR_LOG, newLogFile);
    console.log(`📝 Log rotacionado: ${newLogFile}`);
    
    // Limpar logs antigos (manter apenas os 5 mais recentes)
    limparLogsAntigos();
  } catch (e) {
    console.error('Erro ao rotacionar log:', e);
  }
}

// Limpar logs antigos
function limparLogsAntigos() {
  try {
    const arquivos = fs.readdirSync(LOG_DIR)
      .filter(file => file.startsWith('erros_') && file.endsWith('.log'))
      .map(file => ({
        nome: file,
        caminho: path.join(LOG_DIR, file),
        data: fs.statSync(path.join(LOG_DIR, file)).mtime.getTime()
      }))
      .sort((a, b) => b.data - a.data); // Ordenar do mais recente para o mais antigo
    
    // Manter apenas os 5 logs mais recentes
    if (arquivos.length > 5) {
      arquivos.slice(5).forEach(arquivo => {
        fs.unlinkSync(arquivo.caminho);
        console.log(`🗑️ Log antigo removido: ${arquivo.nome}`);
      });
    }
  } catch (e) {
    console.error('Erro ao limpar logs antigos:', e);
  }
}

// Enviar notificação para o usuário via WhatsApp
function enviarNotificacaoWhatsApp(client, mensagem) {
  try {
    if (client && config.NOTIFICACOES_ATIVAS) {
      // Enviar para o número do administrador (pode ser configurado no config)
      client.sendMessage(config.NAMORADA, `🤖 [SISTEMA] ${mensagem}`);
      return true;
    }
    return false;
  } catch (e) {
    console.error('Erro ao enviar notificação WhatsApp:', e);
    return false;
  }
}

// Obter resumo de erros recentes
function obterResumoErros() {
  try {
    garantirDiretorioLogs();
    
    if (fs.existsSync(ERROR_LOG)) {
      const conteudo = fs.readFileSync(ERROR_LOG, 'utf8');
      const linhas = conteudo.split('\n').filter(Boolean);
      
      // Retornar os últimos 10 erros
      return linhas.slice(-10).map(linha => {
        const match = linha.match(/\[(.*?)\] \[(.*?)\] (.*)/);
        if (match) {
          return {
            timestamp: match[1],
            categoria: match[2],
            mensagem: match[3]
          };
        }
        return { mensagem: linha };
      });
    }
    
    return [];
  } catch (e) {
    console.error('Erro ao obter resumo de erros:', e);
    return [];
  }
}

module.exports = {
  registrarErro,
  enviarNotificacaoWhatsApp,
  obterResumoErros
};