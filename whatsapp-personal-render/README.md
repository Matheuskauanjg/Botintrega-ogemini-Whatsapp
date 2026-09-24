# WhatsApp Personal Bridge

Bridge privado para conectar um WhatsApp pessoal via `whatsapp-web.js`, publicar a API como Web Service no Render e expor um QR Code para autenticação.

## Endpoints

- `GET /health` — saúde do serviço (público)
- `GET /qr?key=SEU_QR_SECRET` — página do QR Code
- `GET /openapi.json` — OpenAPI para integração
- `GET /api/status` — status da sessão
- `GET /api/chats?limit=30` — chats recentes
- `GET /api/chats/:chatId/messages?limit=30` — mensagens de um chat
- `GET /api/search?q=texto` — busca em mensagens recentes
- `POST /api/send` — envia uma mensagem individual

Os endpoints `/api/*` exigem `Authorization: Bearer <API_TOKEN>` ou `x-api-key: <API_TOKEN>`.

## Deploy no Render

O repositório possui um `render.yaml` na raiz desta branch. Crie um Blueprint/Web Service a partir da branch `whatsapp-personal-render`.

Variáveis obrigatórias no Render:

```env
API_TOKEN=gere-um-token-grande-e-aleatorio
QR_SECRET=gere-outra-chave-grande-e-aleatoria
PUBLIC_BASE_URL=https://SEU-SERVICO.onrender.com
TUNNEL_TOKEN=configure-se-o-seu-plugin-tunel-utilizar-este-token
WWEBJS_CLIENT_ID=personal
WWEBJS_AUTH_PATH=/var/data/.wwebjs_auth
PUPPETEER_EXECUTABLE_PATH=/usr/bin/chromium
```

Não grave tokens reais no GitHub.

## Primeiro login

Depois do deploy:

1. Acesse `https://SEU-SERVICO.onrender.com/health` e confirme que o serviço está vivo.
2. Abra `https://SEU-SERVICO.onrender.com/qr?key=SEU_QR_SECRET`.
3. No celular, abra WhatsApp → Dispositivos conectados → Conectar dispositivo.
4. Escaneie o QR exibido pela página.
5. Aguarde a página mostrar `WhatsApp conectado`.

A página do QR atualiza automaticamente a cada 5 segundos.

## Testar a API

Status:

```bash
curl -H "Authorization: Bearer SEU_API_TOKEN" \
  https://SEU-SERVICO.onrender.com/api/status
```

Chats:

```bash
curl -H "Authorization: Bearer SEU_API_TOKEN" \
  "https://SEU-SERVICO.onrender.com/api/chats?limit=20"
```

Enviar mensagem:

```bash
curl -X POST \
  -H "Authorization: Bearer SEU_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"to":"5541999999999","message":"Teste"}' \
  https://SEU-SERVICO.onrender.com/api/send
```

## Persistência

`LocalAuth` grava a sessão em `/var/data/.wwebjs_auth`. O Blueprint inclui um Persistent Disk de 1 GB para evitar que a sessão desapareça em redeploys/restarts.

## Segurança

- mantenha `API_TOKEN`, `QR_SECRET` e qualquer token de túnel somente nas variáveis secretas do Render;
- não exponha a URL `/qr` sem chave;
- não compartilhe a sessão `.wwebjs_auth`;
- para ferramentas de IA, configure envio de mensagens como ação que exige sua confirmação;
- evite automação em massa: `whatsapp-web.js` usa um cliente não oficial baseado no WhatsApp Web.
