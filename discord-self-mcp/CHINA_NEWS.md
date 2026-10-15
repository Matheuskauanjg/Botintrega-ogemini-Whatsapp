# Boletim da China — Discord

Serviço instalado automaticamente pelo módulo \`bridge_chain.py\` do \`discord-self-mcp\`.

## Funcionamento

- **Canal:** https://discord.com/channels/1251266361569710222/1560415874114920559
- **Intervalo:** 2 horas após a última notícia publicada com sucesso.
- **Rotatividade:** notícias nacionais (\`国内\`), regionais (\`地方\`, alternando províncias) e celebridades/fofocas (\`娱乐\`).
- **Fontes:** pesquisas RSS do Bing News e Google News em chinês; o link e o título são extraídos de itens reais, publicados nas últimas 96 horas.
- **Imagem:** retirada dos metadados da matéria (\`og:image\` / \`twitter:image\`) ou da mídia indicada no RSS. A foto é baixada e anexada ao Discord para não depender de hotlink. Sem foto validada, a notícia é ignorada.
- **Tradução:** título e resumo traduzidos para português brasileiro pelos provedores já existentes (Gemini 1, Gemini 2, NVIDIA e Groq, nessa ordem). Não inventar informações além do título e resumo.
- **Tópico:** cada mensagem em chinês origina um tópico público com a tradução em português e o mesmo link de fonte.
- **Não repetição:** URLs publicadas em SQLite e verificação do histórico recente do canal.
- **Falhas:** não publica quando faltar fonte, imagem ou tradução; tenta novamente depois, sem bloquear as outras funções.

## Configuração (Render)

A rotina inicia por padrão com \`BRIDGE_APP_MODULE=bridge_chain\`. As variáveis abaixo são opcionais, pois já têm valores padrão:

\`\`\`dotenv
CHINA_NEWS_ENABLED=true
CHINA_NEWS_CHANNEL_ID=1560415874114920559
CHINA_NEWS_INTERVAL_SECONDS=7200
CHINA_NEWS_MAX_AGE_HOURS=96
CHINA_NEWS_DB_PATH=/data/china_news.sqlite3
\`\`\`

A conta precisa poder visualizar e escrever no canal, **criar tópicos públicos e enviar mensagens nos tópicos**, além de anexar arquivos. O destino precisa ser um canal de texto tradicional; posts em canais de fórum exigem outra integração.

O agendador roda somente enquanto o processo \`meu-discord\` estiver ativo. Hospedagens que suspendem instâncias impedem publicações nesse período. Para evitar perda de histórico entre reinícios, monte um volume persistente em \`/data\` (se disponível). Sem volume, o código verifica notícias recentes do histórico do Discord, mas não garante histórico de URLs entre reinícios.

## Diagnóstico

Acesse \`GET /api/china-news/status\` no servidor bridge (interno), com \`Authorization: Bearer <API_TOKEN>\`. O endpoint informa configuração, última publicação, tentativa, notícia e erro. No log do serviço, busque \`[ChinaNews]\`.

> **Aviso do Discord:** automatizar contas pessoais (selfbots) contraria os Termos de Serviço da plataforma e pode levar a suspensão da conta. Para uma operação de longo prazo, recomenda-se migrar o publicador para um bot oficial com token de aplicação.
