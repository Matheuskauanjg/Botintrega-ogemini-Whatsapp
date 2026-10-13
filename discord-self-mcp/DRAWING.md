# Desenhos contextualizados do Lobão/Greed

O módulo bridge_draw.py usa as falas e a memória recente do canal de auto-resposta como referência para desenhar uma cena e publicá-la no Discord como anexo. Ele é carregado por bridge_chain.py e não altera o fluxo principal de conversa.

## Como usar

No canal 1554920683786739712, as mensagens !desenhar, !desenhar um duelo de magos, Greed, desenha essa conversa ou Lobão, ilustra esse papo pedem um desenho manual. Para diagnóstico, envie `!desenho status` nesse mesmo canal. O comando retorna quais chaves estão configuradas (sem exibi-las) e os códigos HTTP das últimas tentativas. O pedido de desenho é interceptado antes da resposta automática de texto; a resposta será uma imagem anexada (ou um aviso de erro, caso as APIs falhem). Além disso, com o modo automático ligado, a cada 12 falas do próprio Lobão no canal, ele tenta ilustrar a conversa recente, respeitando o intervalo mínimo e o limite diário.

O desenho usa as últimas mensagens, não apenas a última fala. O estilo é decidido com base no assunto: por exemplo, RPG vira fantasia, uma piada pode virar um quadrinho/cartoon, e conversas cotidianas viram cenas ilustradas. A imagem não reproduz capturas de tela da conversa.

## Configuração no Render

No serviço do Discord, defina ou confira as variáveis de ambiente:

| Variável | Padrão | Descrição |
| --- | --- | --- |
| DRAW_ENABLED | true | Ativa o módulo |
| DRAW_AUTO_ENABLED | true | Desenhos espontâneos a partir de falas do próprio bot |
| DRAW_CHANNEL_ID | 1554920683786739712 | Canal de destino |
| DRAW_GUILD_ID | 1251266361569710222 | Servidor permitido |
| NVIDIA_API_KEY | (definido no Render) | Reutiliza a chave NVIDIA do bot para imagem |
| NVIDIA_IMAGE_API_KEY | (opcional) | Se definido, tem prioridade sobre NVIDIA_API_KEY |
| NVIDIA_IMAGE_MODEL | stabilityai/stable-diffusion-3-medium | Também aceita black-forest-labs/flux.2-klein-4b |
| NVIDIA_IMAGE_FALLBACK_MODEL | black-forest-labs/flux.1-schnell | Segunda tentativa da NVIDIA se a primeira falhar; pode definir vazio para desativar |
| DRAW_MODEL | gemini-3.1-flash-image | Modelo de imagem Gemini como fallback |
| GEMINI_IMAGE_API_KEY | (opcional) | Chave dedicada; tenta GEMINI_API_KEY e GEMINI_API_KEY_2 se ausente/falhar |
| DRAW_AUTO_EVERY_REPLIES | 12 | Quantas falas do próprio bot para tentar desenho automático |
| DRAW_COOLDOWN_SECONDS | 3600 | Intervalo mínimo entre tentativas automáticas, em segundos |
| DRAW_DAILY_LIMIT | 4 | Número máximo de imagens enviadas por dia UTC, por processo |

A geração tenta PRIMEIRO NVIDIA NIM Image usando NVIDIA_IMAGE_API_KEY ou NVIDIA_API_KEY. O padrão utiliza a API Stable Diffusion 3 Medium; você pode mudar para FLUX.2 Klein 4B. Se a NVIDIA falhar ou não estiver configurada, tenta a API Gemini Image. Esses serviços exigem que a chave tenha acesso ao modelo e cotas suficientes. **Ter uma chave NVIDIA para texto não garante acesso à geração de imagens.** Os modelos podem ter cobrança e limites separados.

Configure os segredos só no painel Render. A aplicação não armazena as chaves no GitHub. O comando deve retornar um arquivo de imagem, nunca uma resposta de texto fingindo que desenhou.

Teste sem gastar imagens com python -m unittest test_bridge_draw.py no diretório discord-self-mcp. O endpoint autenticado GET /api/drawing/status mostra `nvidiaConfigured`, `geminiConfigured`, `nvidiaImageModel`, `stats.lastProvider`, últimos erros, jobs em execução e o contador. É importante verificar o log do Render e fazer uma geração manual antes de confiar na rotina autônoma.

**Atenção:** Os contadores diários e de cooldown ficam na memória do processo e reiniciam se o serviço for reiniciado. O envio de imagens exige permissões para anexar arquivos ao canal. Selfbots de contas pessoais não são compatíveis com as regras de uso do Discord; para operação estável, prefira migrar para um bot oficial.


## Se aparecer erro ao desenhar

O bot mostra `NVIDIA: HTTP 401/403/404/429/422` (quando houver) e o resultado da tentativa no Gemini. `401`: chave inválida; `403`: chave sem permissão para o modelo; `404`: modelo/rota não disponível; `429`: limite de uso. Se ambos falharem, consulte `!desenho status` para identificar qual provedor está negando a geração. O acesso NVIDIA para respostas de texto **não garante** que a mesma chave possa criar imagens.

Os logs detalhados ficam no Render, mas não exibimos corpo bruto das respostas nem credenciais no Discord. Para testar funcionamento com a plataforma, é necessário ter permissão da chave aos modelos de imagem e publicar a branch atualizada no serviço.
