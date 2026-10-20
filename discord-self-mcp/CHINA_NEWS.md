# Boletim da China — Discord

Serviço instalado automaticamente pelo módulo `bridge_chain.py` do `discord-self-mcp`.

## Funcionamento

- **Canal:** https://discord.com/channels/1251266361569710222/1560415874114920559
- **Intervalo:** 2 horas após a última notícia publicada com sucesso.
- **Rotatividade:** notícias nacionais (`国内`), regionais (`地方`, alternando províncias) e celebridades/fofocas (`娱乐`).
- **Fontes:** pesquisas RSS do Bing News e Google News em chinês; o link e o título são extraídos de itens reais, publicados nas últimas 96 horas.
- **Imagem:** retirada dos metadados da matéria (`og:image` / `twitter:image`) ou da mídia indicada no RSS. A foto é baixada e anexada ao Discord para não depender de hotlink. Se a notícia não tiver uma foto válida, o bot pesquisa imagens tematicamente relacionadas no Openverse e Wikimedia Commons (com licenças abertas), baixa a foto e adiciona a identificação **imagem ilustrativa, não é foto do acontecimento**, com autor, licença e link da obra nas versões chinesa e portuguesa. Fotos com aparência de logotipo, banner ou imagens muito pequenas são rejeitadas, e um hash perceptual impede reutilizar fotos iguais ou quase iguais em notícias diferentes. As buscas alternativas usam palavras ligadas ao assunto real, não paisagens genéricas da China. Sem foto original nem alternativa licenciada validada, passa para outra notícia.
- **Redação expandida:** extrai o conteúdo da reportagem original e gera um resumo autoral em chinês (normalmente 4 a 6 parágrafos, dependendo dos fatos realmente disponíveis). Mantém o post principal abaixo de 2.000 caracteres, com a URL integral e a imagem.
- **Tradução:** tradução para português brasileiro de todos os parágrafos produzidos em chinês, pelos provedores existentes (Gemini 1, Gemini 2, NVIDIA e Groq, nessa ordem). O tópico pode conter mais de uma mensagem para preservar a tradução completa. Não inventar informações além da reportagem original.
- **Tópico:** cada mensagem em chinês origina um tópico público com a tradução em português e o mesmo link de fonte.
- **Marcação contextual:** até duas pessoas por postagem, apenas membros do servidor cujo nome completo ou interesse explicitamente cadastrado combine com a manchete/resumo. No máximo **uma menção por pessoa por dia**, no fuso `America/Sao_Paulo`, mesmo em publicações manuais; controla em SQLite e consulta o histórico do canal após reinícios. Sem vínculo confiável não marca ninguém.
- **Não repetição:** URLs publicadas em SQLite e verificação do histórico recente do canal.
- **Falhas:** não publica quando faltar fonte, imagem ou tradução; tenta novamente depois, sem bloquear as outras funções.

## Configuração (Render)

**Chat:** o Greed só conversa no canal https://discord.com/channels/1251266361569710222/1554920683786739712; menções em outros canais não ativam o chat. No canal indicado, uma menção inicia uma conversa com continuidade temporária.

A rotina inicia por padrão com `BRIDGE_APP_MODULE=bridge_chain`. As variáveis abaixo são opcionais, pois já têm valores padrão:

```dotenv
CHINA_NEWS_ENABLED=true
CHINA_NEWS_CHANNEL_ID=1560415874114920559
CHINA_NEWS_INTERVAL_SECONDS=7200
CHINA_NEWS_MAX_AGE_HOURS=96
CHINA_NEWS_EXTERNAL_IMAGES=true
CHINA_NEWS_MENTIONS_ENABLED=true
CHINA_NEWS_MENTION_TIME_ZONE=America/Sao_Paulo
CHINA_NEWS_MAX_MENTIONS_PER_POST=2
# Associe manualmente os IDs de membros aos interesses; formato JSON em uma linha:
# CHINA_NEWS_MENTION_RULES={"123456789012345678":["足球","世界杯","futebol"]}
CHINA_NEWS_DB_PATH=/data/china_news.sqlite3
```

A conta precisa poder visualizar e escrever no canal, **criar tópicos públicos e enviar mensagens nos tópicos**, além de anexar arquivos. O destino precisa ser um canal de texto tradicional; posts em canais de fórum exigem outra integração.

O agendador roda somente enquanto o processo `meu-discord` estiver ativo. Hospedagens que suspendem instâncias impedem publicações nesse período. Para evitar perda de histórico entre reinícios, monte um volume persistente em `/data` (se disponível). Sem volume, o código verifica notícias recentes do histórico do Discord, mas não garante histórico de URLs entre reinícios.

## Diagnóstico

Acesse `GET /api/china-news/status` no servidor bridge (interno), com `Authorization: Bearer <API_TOKEN>`. O endpoint informa configuração, última publicação, tentativa, notícia e erro. No log do serviço, busque `[ChinaNews]`.

> **Aviso do Discord:** automatizar contas pessoais (selfbots) contraria os Termos de Serviço da plataforma e pode levar a suspensão da conta. Para uma operação de longo prazo, recomenda-se migrar o publicador para um bot oficial com token de aplicação.

## Comando manual

Digite no **servidor configurado**:

- `!noticia` — publica imediatamente a próxima categoria do rodízio.
- `!noticia nacional` — notícia nacional chinesa.
- `!noticia provincia` (ou `!noticia província`) — notícia de província chinesa.
- `!noticia fofoca` — notícia sobre entretenimento, celebridades ou fofocas.

O comando publica no canal de notícias configurado, independentemente do canal onde for enviado, e responde com um link para a mensagem criada. Pula a espera de duas horas, mas **não repete matérias** e exige imagem original ou ilustrativa licenciada, fonte e tradução. Um novo envio manual só pode começar após 30 segundos, por padrão. **A publicação manual não adia mais a publicação automática de duas em duas horas.** O relógio da rotina considera somente as postagens agendadas, inclusive após reinício, consultando o histórico do canal. No status, os campos `lastScheduledPosted` e `nextScheduledDue` mostram o agendamento em UTC.

Por segurança, apenas a própria conta conectada, o dono do servidor ou os IDs relacionados em `CHINA_NEWS_ADMIN_IDS` podem ativá-lo. Os demais comandos do Greed são preservados.

Variáveis opcionais: `CHINA_NEWS_GUILD_ID=1251266361569710222`, `CHINA_NEWS_ADMIN_IDS=123456789012345678` (vários separados por vírgula), `CHINA_NEWS_MANUAL_COOLDOWN_SECONDS=30`.
