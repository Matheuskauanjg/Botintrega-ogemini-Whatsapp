# WhatsApp Personal Bridge

Bridge privado em Node.js + Baileys para conectar um WhatsApp pessoal ao ChatGPT via MCP/OAuth.

## Arquitetura

- Baileys / REST interno: `10001`
- MCP/OAuth: `10002`
- transcrição de áudio: `10003`
- proxy público: `PORT` (Railway: `10000`)
- sessão Baileys persistente: `/data/baileys_auth`
- histórico SQLite persistente: `/data/whatsapp.sqlite`
- estado do auto reply: `/data/whatsapp-auto-reply.json`

## Recursos

- listar chats e ler mensagens;
- pesquisar histórico persistente;
- ouvir/transcrever áudios com Groq Whisper;
- enviar texto e imagem;
- responder/citar uma mensagem por `messageId`;
- mencionar JIDs ou mencionar automaticamente o autor de uma mensagem;
- reagir a mensagens com emoji;
- auto reply orientado a eventos do Baileys, com debounce curto e concorrência configurável;
- persistência de conversas, mensagens, contatos e mapeamentos LID → número.

## Endpoints principais

- `GET /health`
- `GET /qr`
- `GET /api/status`
- `GET /api/chats?limit=30`
- `GET /api/chats/:chatId/messages?limit=30`
- `GET /api/search?q=texto`
- `GET /api/db-stats`
- `POST /api/send`
- `POST /api/react`
- `POST /api/send-image`
- `POST /api/audio`

Os endpoints `/api/*` exigem `Authorization: Bearer <API_TOKEN>` ou `x-api-key: <API_TOKEN>`.

## Railway

Configure um volume persistente montado em `/data` e use a branch `whatsapp-personal-render`.

Variáveis principais:

```env
PORT=10000
PUBLIC_BASE_URL=https://SEU-SERVICO.up.railway.app
API_TOKEN=gere-um-token-grande-e-aleatorio
MCP_LOGIN_SECRET=gere-outra-chave-grande-e-aleatoria

BAILEYS_AUTH_PATH=/data/baileys_auth
WHATSAPP_DB_PATH=/data/whatsapp.sqlite
AUTO_REPLY_STATE_PATH=/data/whatsapp-auto-reply.json

AUTO_REPLY_CONTROL_JID=SEU_LID@lid
AUTO_REPLY_DELAY_MIN_MS=250
AUTO_REPLY_DELAY_MAX_MS=750
AUTO_REPLY_DEBOUNCE_MS=350
AUTO_REPLY_CONCURRENCY=4

GROQ_API_KEY=configure-no-painel
GROQ_REPLY_MODEL=openai/gpt-oss-20b
GROQ_TRANSCRIBE_MODEL=whisper-large-v3-turbo
GROQ_TRANSCRIBE_LANGUAGE=pt
```

Não grave tokens reais no GitHub.

## Enviar uma mensagem simples

```bash
curl -X POST \
  -H "Authorization: Bearer SEU_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"to":"5541999999999","message":"Teste"}' \
  https://SEU-SERVICO.up.railway.app/api/send
```

## Responder/citar uma mensagem

Primeiro obtenha o `id` usando `read_whatsapp_messages` ou `/api/chats/:chatId/messages`.

```json
{
  "to": "120363000000000000@g.us",
  "message": "respondendo essa aqui",
  "replyToMessageId": "3EB0..."
}
```

## Responder e marcar o autor

```json
{
  "to": "120363000000000000@g.us",
  "message": "beleza kkk",
  "replyToMessageId": "3EB0...",
  "mentionAuthorOfMessageId": "3EB0..."
}
```

## Reagir

```json
{
  "to": "120363000000000000@g.us",
  "messageId": "3EB0...",
  "emoji": "😂"
}
```

## Auto reply

O auto reply não varre mais todos os chats a cada ciclo. O bridge emite um evento quando `messages.upsert` chega, aplica um pequeno debounce e processa conversas em paralelo.

Padrões atuais:

- debounce: `350 ms`;
- atraso humano antes do envio: `250–750 ms`;
- concorrência: `4` conversas;
- intervalo entre partes: `150–350 ms`.

Comandos enviados na conversa de controle:

- `auto on`
- `auto off`
- `auto status`

## Persistência

O SQLite mantém histórico de texto e metadados entre deploys/restarts. Áudio bruto continua dependendo do cache de mídia da sessão para download/transcrição, mas o registro da mensagem permanece no histórico.

## Observação

Baileys é um cliente não oficial baseado no protocolo do WhatsApp Web. Mudanças do WhatsApp podem exigir ajustes futuros.
