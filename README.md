# 🤖 Bot WhatsApp + Gemini AI (v2.0.0)

Bot do WhatsApp inteligente que responde automaticamente para sua namorada usando IA Gemini. Com sistema de personalização completo, backup automático, notificações e aprendizado adaptativo.

## 🚀 Como usar

### 1. Instalar dependências
```
npm install
```

### 2. Configurar variáveis de ambiente
Crie um arquivo `.env` na raiz do projeto:
```env
GEMINI_API_KEY=sua_chave_do_gemini_aqui
NAMORADA_NUMBER=559999999999@c.us
```

### 3. Personalizar o bot para sua namorada

#### Arquivo de Personalização (`src/personalizacao/personalizacao.js`)
Edite as seguintes informações sobre sua namorada:
```javascript
const personalizacaoNamorada = {
  nomeNamorada: "NOME_DA_SUA_NAMORADA_AQUI",  // 🡸 Mude para o nome dela
  nomeUsuario: "SEU_NOME_AQUI",                // 🡸 Mude para o seu nome
  
  caracteristicasNamorada: {
    personalidade: "Descreva a personalidade dela aqui",
    sensibilidade: "Descreva o que ela gosta/não gosta",
    gostos: "Liste os gostos dela",
    // ... outros campos
  }
};
```

#### Templates de Mensagens (`src/personalizacao/mensagens.js`)
Personalize as respostas automáticas:
```javascript
const templatesMensagens = {
  mensagensBomDia: [
    "Bom dia, meu amor! 🌅",
    "Acordei pensando em você ❤️"
    // Adicione mais mensagens
  ],
  
  mensagensBoaNoite: [
    "Boa noite, meu bem! 🌙",
    "Durma bem, te amo! 💕"
    // Adicione mais mensagens
  ]
};
```

#### Palavras-chave Personalizadas
Adicione palavras que sua namorada usa frequentemente:
```javascript
const palavrasChave = {
  "palavra_dela": {
    respostas: ["resposta_personalizada_1", "resposta_personalizada_2"],
    probabilidade: 0.8
  }
  // Adicione mais palavras-chave
};
```bash
npm install
```

### 2. Configurar variáveis de ambiente
Crie um arquivo `.env` na raiz do projeto:
```env
GEMINI_API_KEY=sua_chave_do_gemini_aqui
NAMORADA_NUMBER=559999999999@c.us
```

### 4. Executar o bot
```bash
npm start
```

### 5. Conectar WhatsApp
- Um QR Code aparecerá no terminal
- Escaneie com seu WhatsApp (WhatsApp Web)
- Aguarde a confirmação de conexão

## ✨ Funcionalidades

- 🤖 Responde automaticamente mensagens da sua namorada
- 💕 Usa IA do Gemini para responder como você
- 🎨 **NOVO:** Sistema completo de personalização para sua namorada
- 📝 **NOVO:** Templates de mensagens personalizáveis (bom dia, boa noite)
- 🔑 **NOVO:** Palavras-chave com respostas automáticas
- ⚙️ **NOVO:** Configuração de personalidade e características
- 🔒 Mantém autenticação local do WhatsApp
- 📱 Interface simples via terminal
- 💾 Sistema de backup automático das conversas
- 🔔 Sistema de notificações para erros
- 📊 Sistema de aprendizado com feedback estruturado
- 🧠 Comandos de treinamento via WhatsApp

## ⚙️ Configuração

### Número da namorada
O número deve estar no formato: `55XXYYYYYYYY@c.us`
- 55 = código do Brasil
- XX = DDD
- YYYYYYYY = número do celular

### Chave da API Gemini
1. Acesse [Google AI Studio](https://makersuite.google.com/app/apikey)
2. Crie uma nova chave
3. Cole no arquivo `.env`

## 🛠️ Scripts disponíveis

- `npm start` - Executa o bot
- `npm run dev` - Executa com auto-reload (desenvolvimento)
- `npm test` - Testa a conexão com a API do Gemini
- `npm run backup` - Força um backup manual das conversas
- `npm run restore` - Restaura um backup anterior (interativo)

## 📝 Logs e Comandos

### Terminal
O bot mostra no terminal:
- ✅ Status de conexão
- 📩 Mensagens recebidas
- ❤️ Respostas enviadas
- ❌ Erros (se houver)

### Comandos no Terminal
- `backup` - Força um backup manual
- `status` - Mostra status do sistema
- `erros` - Mostra últimos erros registrados
- `relatorio` - Mostra relatório de desempenho
- `sair` - Encerra o bot

### Comandos via WhatsApp
Envie estes comandos para treinar o bot:
- `+boa` - Marca sua última mensagem como "boa" para aprendizado
- `+dica [texto]` - Adiciona uma dica de comportamento
- `+relatorio` - Recebe um relatório de desempenho
- `+backup` - Força um backup das conversas

## 🗂️ Estrutura do Projeto

```
/
├── src/                    # Código-fonte
│   ├── config/             # Configurações
│   ├── services/           # Serviços (WhatsApp, IA)
│   ├── test/               # Testes
│   ├── utils/              # Utilitários
│   └── index.js            # Ponto de entrada
├── backups/                # Backups automáticos (criado automaticamente)
├── logs/                   # Logs de erros (criado automaticamente)
├── .env                    # Variáveis de ambiente
├── package.json            # Dependências
└── README.md               # Documentação
```

## 🔧 Solução de problemas

### Bot não conecta
- Verifique se o WhatsApp está ativo no celular
- Tente escanear o QR Code novamente
- Reinicie o bot
- Verifique os logs de erro com o comando `erros`

### Não responde mensagens
- Verifique se o número está correto no formato `@c.us`
- Confirme se a chave da API Gemini está válida com `npm test`
- Verifique os logs de erro com o comando `erros`

### Respostas estranhas
- A IA pode demorar para "aprender" seu estilo
- Use o comando `+boa` para marcar boas respostas
- Use o comando `+dica` para adicionar dicas de comportamento

### Problemas com backups
- Verifique se a pasta `backups/` existe e tem permissões
- Force um backup manual com `npm run backup`
- Verifique os logs de erro relacionados a backups

## ⚠️ Importante

- Mantenha a chave da API segura
- Não compartilhe o arquivo `.env`
- Use com responsabilidade
- O bot funciona apenas com o número configurado
- Backups são feitos automaticamente a cada 30 minutos

## 📞 Suporte

Se tiver problemas, verifique:
1. Se todas as dependências foram instaladas
2. Se a chave da API está correta (use `npm test`)
3. Se o número está no formato correto
4. Se o WhatsApp está conectado
5. Os logs de erro com o comando `erros`

---

💕 **Feito com amor para responder sua namorada automaticamente!**
