# Desenhos contextualizados do Lobão/Greed

O módulo bridge_draw.py usa as falas e a memória recente do canal de auto-resposta como referência para desenhar uma cena e publicá-la no Discord como anexo. Ele é carregado por bridge_chain.py e não altera o fluxo principal de conversa.

## Como usar

No canal 1554920683786739712, as mensagens !desenhar, !desenhar um duelo de magos, Greed, desenha essa conversa ou Lobão, ilustra esse papo pedem um desenho manual. Além disso, com o modo automático ligado, a cada 12 falas do próprio Lobão no canal, ele tenta ilustrar a conversa recente, respeitando o intervalo mínimo e o limite diário.

O desenho usa as últimas mensagens, não apenas a última fala. O estilo é decidido com base no assunto: por exemplo, RPG vira fantasia, uma piada pode virar um quadrinho/cartoon, e conversas cotidianas viram cenas ilustradas. A imagem não reproduz capturas de tela da conversa.

## Configuração no Render

No serviço do Discord, defina ou confira as variáveis de ambiente:

| Variável | Padrão | Descrição |
| --- | --- | --- |
| DRAW_ENABLED | true | Ativa o módulo |
| DRAW_AUTO_ENABLED | true | Desenhos espontâneos a partir de falas do próprio bot |
| DRAW_CHANNEL_ID | 1554920683786739712 | Canal de destino |
| DRAW_GUILD_ID | 1251266361569710222 | Servidor permitido |
| DRAW_MODEL | gemini-3.1-flash-image | Modelo de imagem |
| GEMINI_IMAGE_API_KEY | (opcional) | Chave dedicada; tenta GEMINI_API_KEY e GEMINI_API_KEY_2 se ausente/falhar |
| DRAW_AUTO_EVERY_REPLIES | 12 | Quantas falas do próprio bot para tentar desenho automático |
| DRAW_COOLDOWN_SECONDS | 3600 | Intervalo mínimo entre tentativas automáticas, em segundos |
| DRAW_DAILY_LIMIT | 4 | Número máximo de imagens enviadas por dia UTC, por processo |

O modelo de imagem pode exigir acesso à API e faturamento habilitado. As cotas e a cobrança do Gemini são separadas das chamadas de texto. A aplicação não armazenará sua chave no repositório; use apenas as variáveis de ambiente no Render.

Teste sem gastar imagens com python -m unittest test_bridge_draw.py no diretório discord-self-mcp. O endpoint autenticado GET /api/drawing/status mostra se há chave, jobs em execução, contador e último erro. É importante verificar o log do Render e fazer uma geração manual antes de confiar na rotina autônoma.

**Atenção:** Os contadores diários e de cooldown ficam na memória do processo e reiniciam se o serviço for reiniciado. O envio de imagens exige permissões para anexar arquivos ao canal. Selfbots de contas pessoais não são compatíveis com as regras de uso do Discord; para operação estável, prefira migrar para um bot oficial.
