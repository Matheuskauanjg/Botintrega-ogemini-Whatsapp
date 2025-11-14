# Personalização do WhatsApp Bot

## Como personalizar para sua namorada

### 1. Arquivo de Personalização (`src/personalizacao/personalizacao.js`)

Edite as seguintes variáveis no arquivo:

```javascript
const personalizacaoNamorada = {
  // Informações básicas
  nomeNamorada: "NOME_DA_SUA_NAMORADA_AQUI",
  nomeUsuario: "SEU_NOME_AQUI",
  
  // Características da namorada
  caracteristicasNamorada: {
    personalidade: "Descreva a personalidade dela",
    sensibilidade: "Como ela lida com emoções",
    dependencia: "Nível de dependência no relacionamento",
    humor: "Tipo de humor dela",
    rotina: "Rotinas compartilhadas",
    fisico: "Características físicas (opcional)",
    gostos: ["lista", "de", "gostos", "dela"]
  },
  
  // Resto das configurações...
};
```

### 2. Arquivo de Mensagens (`src/personalizacao/mensagens.js`)

Personalize as mensagens de acordo com o estilo do seu relacionamento:

```javascript
const mensagensPersonalizadas = {
  templates: {
    amor: ["mensagem1", "mensagem2"],
    bom_dia: ["bom dia amor", "dia lindo"],
    // etc...
  },
  
  palavrasChave: {
    "palavra": ["resposta1", "resposta2"],
    // etc...
  }
};
```

### 3. Arquivo .env

Configure as variáveis de ambiente:

```env
GEMINI_API_KEY=sua_chave_gemini_aqui
NAMORADA_NUMBER=5582XXXXXXX@c.us  // Número da sua namorada
MEU_NUMBER=5541XXXXXXX@c.us       // Seu número
```

## Exemplos de Personalização

### Estilo Romântico Tradicional
```javascript
personalidade: "Romântica e carinhosa, gosta de elogios",
apelidos: ["amor", "vida", "meu bem", "querida"],
```

### Estilo Engraçado/Descontraído
```javascript
personalidade: "Brincalhona e divertida, gosta de piadas",
apelidos: ["gatinha", "linda", "minha crazy"],
```

### Estilo Tímido/Reservado
```javascript
personalidade: "Tímida e reservada, prefere mensagens simples",
apelidos: ["amor", "querida"],
```

## Dicas Importantes

1. **Seja específico**: Quanto mais detalhes, melhor o bot vai se comportar
2. **Use exemplos reais**: Baseie-se em conversas reais do seu relacionamento
3. **Teste gradualmente**: Comece com poucas mudanças e vá ajustando
4. **Observe padrões**: Note quais mensagens funcionam melhor e ajuste
5. **Mantenha natural**: Evite ser muito artificial ou forçado

## Arquivos de Exemplo

- `personalizacao.js` - Configurações principais da personalidade
- `mensagens.js` - Templates de mensagens e respostas automáticas
- `.env` - Configurações sensíveis (números, API keys)

## Suporte

Se precisar de ajuda para personalizar, edite os arquivos mencionados acima.