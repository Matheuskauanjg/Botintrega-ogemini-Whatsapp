import ast
import asyncio
import math
import os
import random
import re
import time
import unicodedata
from collections import defaultdict, deque
from typing import Any

import httpx
from fastapi import Depends

import bridge_download as stack

app = stack.app
music = stack.music
base = music.base
client = base.client

AUTO_REPLY_ENABLED = os.getenv("AUTO_REPLY_ENABLED", "true").strip().lower() in {"1", "true", "yes", "on"}
AUTO_REPLY_GUILD_ID = os.getenv("AUTO_REPLY_GUILD_ID", "1251266361569710222").strip()
AUTO_REPLY_CHANNEL_ID = os.getenv("AUTO_REPLY_CHANNEL_ID", "1251266362014302248").strip()
AUTO_REPLY_TRIGGERS = [
    item.casefold().strip()
    for item in os.getenv("AUTO_REPLY_TRIGGERS", "grade,greed").split(",")
    if item.strip()
]
AUTO_REPLY_COOLDOWN_SECONDS = max(0.0, float(os.getenv("AUTO_REPLY_COOLDOWN_SECONDS", "5")))
AUTO_REPLY_DUPLICATE_WINDOW_SECONDS = max(0.0, float(os.getenv("AUTO_REPLY_DUPLICATE_WINDOW_SECONDS", "12")))
AUTO_REPLY_MAX_CONTEXT = max(1, min(100, int(os.getenv("AUTO_REPLY_MAX_CONTEXT", "60"))))
AUTO_REPLY_CONTEXT_CHARS = max(1500, min(24000, int(os.getenv("AUTO_REPLY_CONTEXT_CHARS", "12000"))))
AUTO_REPLY_MAX_CHARS = max(50, min(1500, int(os.getenv("AUTO_REPLY_MAX_CHARS", "450"))))
AUTO_REPLY_DETAIL_MAX_CHARS = max(AUTO_REPLY_MAX_CHARS, min(6000, int(os.getenv("AUTO_REPLY_DETAIL_MAX_CHARS", "4500"))))
AUTO_REPLY_CODE_MAX_CHARS = max(AUTO_REPLY_DETAIL_MAX_CHARS, min(12000, int(os.getenv("AUTO_REPLY_CODE_MAX_CHARS", "9000"))))
AUTO_REPLY_LONG_MAX_CHARS = AUTO_REPLY_DETAIL_MAX_CHARS
AUTO_REPLY_ALLOW_SELF = os.getenv("AUTO_REPLY_ALLOW_SELF", "true").strip().lower() in {"1", "true", "yes", "on"}
GROQ_AUTO_REPLY_MODEL = os.getenv("GROQ_AUTO_REPLY_MODEL", "openai/gpt-oss-120b").strip()

_last_reply_at: dict[tuple[int, int], float] = defaultdict(float)
_last_seen_message: dict[tuple[int, int, str], float] = {}
_recent_bot_replies: deque[str] = deque(maxlen=12)
auto_reply_stats: dict[str, Any] = {
    "matched": 0,
    "replied": 0,
    "skippedCooldown": 0,
    "skippedDuplicate": 0,
    "skippedChannel": 0,
    "groqFailures": 0,
    "smartFallbacks": 0,
    "lastTrigger": None,
    "lastReplyAt": None,
    "lastModelError": None,
}


def _trigger_match(message) -> tuple[bool, str | None]:
    content = (message.content or "").casefold()

    me = client.user
    if me is not None:
        for mentioned in getattr(message, "mentions", []) or []:
            if getattr(mentioned, "id", None) == getattr(me, "id", None):
                return True, "mention"

    for trigger in AUTO_REPLY_TRIGGERS:
        if re.search(rf"(?<!\w){re.escape(trigger)}(?!\w)", content, flags=re.IGNORECASE):
            return True, trigger

    return False, None


def _normalize(value: str) -> str:
    value = unicodedata.normalize("NFKD", value.casefold())
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    return re.sub(r"\s+", " ", value).strip()


def _clean_message_text(message) -> str:
    cleaned = (message.content or "").strip()
    if client.user is not None:
        cleaned = re.sub(rf"<@!?{client.user.id}>", "", cleaned).strip()
    for item in AUTO_REPLY_TRIGGERS:
        cleaned = re.sub(rf"(?<!\w){re.escape(item)}(?!\w)", "", cleaned, flags=re.IGNORECASE).strip()
    return re.sub(r"\s+", " ", cleaned).strip()


def _reply_profile(text: str) -> dict[str, Any]:
    """Choose response length from the user's actual intent."""
    normalized = _normalize(text)
    code_markers = (
        "gere um codigo", "gera um codigo", "gerar um codigo", "crie um codigo",
        "cria um codigo", "faca um codigo", "faz um codigo", "codigo em ",
        "script", "programa em ", "escreva uma funcao", "crie uma funcao",
        "gere uma funcao", "faz uma funcao",
        "em python", "em c++", "em cpp", "em c#", "em csharp",
        "em javascript", "em js", "em typescript", "em ts", "em java",
        "em lua", "em rust", "em golang", "em go", "em php", "em ruby",
    )
    detailed_markers = (
        "explique", "explica", "me explica", "conte", "conta", "me conte",
        "me fala", "fale sobre", "como funciona", "como fazer", "como faco",
        "por que", "porque", "qual ", "quais ", "o que ", "quem ", "quando ",
        "onde ", "passo a passo", "detalhe", "detalha", "analise", "analisa",
        "compare", "compara", "resuma", "resume",
    )
    code_actions = (
        "deixe", "deixa", "aumente", "aumenta", "maior", "melhore", "melhora",
        "corrija", "corrige", "arrume", "arruma", "complete", "completa",
        "continue", "continua", "adicione", "adiciona", "expanda", "expande",
        "converta", "converte", "transforme", "transforma", "reescreva",
        "otimize", "otimiza", "refatore", "refatora",
    )
    mentions_code = bool(re.search(r"\b(?:codigo|code)\b", normalized))
    looks_like_pasted_code = any(token in text for token in ("```", "def ", "print(", "if ", "elif ", "else:", "return ", "#include", "int main(", "function ", "const ", "let "))
    wants_code = (
        any(marker in normalized for marker in code_markers)
        or (mentions_code and any(action in normalized for action in code_actions))
        or (looks_like_pasted_code and any(action in normalized for action in code_actions))
    )
    wants_detail = "?" in text or wants_code or any(marker in normalized for marker in detailed_markers)

    if wants_code:
        return {"kind": "code", "detailed": True, "max_tokens": 2200, "max_chars": AUTO_REPLY_CODE_MAX_CHARS}
    if wants_detail:
        return {"kind": "detailed", "detailed": True, "max_tokens": 900, "max_chars": AUTO_REPLY_DETAIL_MAX_CHARS}
    return {"kind": "casual", "detailed": False, "max_tokens": 220, "max_chars": AUTO_REPLY_MAX_CHARS}


def _format_number(value: float) -> str:
    if abs(value - round(value)) < 1e-10:
        return str(int(round(value)))
    return f"{value:.6f}".rstrip("0").rstrip(".").replace(".", ",")


def _extract_last_number(context: list[dict[str, str]]) -> float | None:
    pattern = re.compile(r"(?<![\w])[-+]?\d+(?:[.,]\d+)?(?![\w])")
    for row in reversed(context):
        matches = pattern.findall(row.get("content", ""))
        if not matches:
            continue
        try:
            return float(matches[-1].replace(",", "."))
        except ValueError:
            continue
    return None


def _safe_eval_expression(expression: str) -> float | None:
    operators = {
        ast.Add: lambda a, b: a + b,
        ast.Sub: lambda a, b: a - b,
        ast.Mult: lambda a, b: a * b,
        ast.Div: lambda a, b: a / b,
        ast.FloorDiv: lambda a, b: a // b,
        ast.Mod: lambda a, b: a % b,
        ast.Pow: lambda a, b: a**b,
    }

    def visit(node: ast.AST) -> float:
        if isinstance(node, ast.Expression):
            return visit(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            value = float(node.value)
            if not math.isfinite(value) or abs(value) > 1e15:
                raise ValueError("number out of range")
            return value
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
            value = visit(node.operand)
            return value if isinstance(node.op, ast.UAdd) else -value
        if isinstance(node, ast.BinOp) and type(node.op) in operators:
            left = visit(node.left)
            right = visit(node.right)
            if isinstance(node.op, ast.Pow) and abs(right) > 12:
                raise ValueError("exponent too large")
            result = operators[type(node.op)](left, right)
            if not math.isfinite(result) or abs(result) > 1e18:
                raise ValueError("result out of range")
            return float(result)
        raise ValueError("unsupported expression")

    try:
        cleaned = expression.replace(",", ".").replace("×", "*").replace("÷", "/").replace("^", "**")
        tree = ast.parse(cleaned, mode="eval")
        return visit(tree)
    except Exception:
        return None


def _smart_fallback_reply(message, trigger: str | None, context: list[dict[str, str]]) -> str:
    author_name = getattr(message.author, "display_name", None) or getattr(message.author, "name", "mano")
    cleaned = _clean_message_text(message)
    normalized = _normalize(cleaned)
    last_number = _extract_last_number(context)

    # Conversation-aware arithmetic such as "multiplique por 2" or "qual a raiz desse número?".
    if last_number is not None:
        match = re.search(r"multiplic(?:a|ar|e|ado)?(?:\s+isso|\s+esse numero)?\s+por\s+(-?\d+(?:[.,]\d+)?)", normalized)
        if match:
            factor = float(match.group(1).replace(",", "."))
            result = last_number * factor
            return f"{_format_number(last_number)} × {_format_number(factor)} = **{_format_number(result)}** 😏"

        match = re.search(r"divid(?:a|ir|e|ido)?(?:\s+isso|\s+esse numero)?\s+por\s+(-?\d+(?:[.,]\d+)?)", normalized)
        if match:
            divisor = float(match.group(1).replace(",", "."))
            if divisor != 0:
                result = last_number / divisor
                return f"{_format_number(last_number)} ÷ {_format_number(divisor)} = **{_format_number(result)}**"

        if "raiz" in normalized and ("desse numero" in normalized or "deste numero" in normalized or "desse valor" in normalized):
            if last_number < 0:
                return "Nos reais não tem raiz quadrada desse número porque ele é negativo 👀"
            result = math.sqrt(last_number)
            return f"A raiz quadrada de {_format_number(last_number)} é **{_format_number(result)}**."

    # Explicit square-root questions.
    match = re.search(r"raiz(?: quadrada)?(?: de| do)?\s*(-?\d+(?:[.,]\d+)?)", normalized)
    if match:
        value = float(match.group(1).replace(",", "."))
        if value < 0:
            return "Nos números reais, raiz quadrada de número negativo não existe 👀"
        return f"√{_format_number(value)} = **{_format_number(math.sqrt(value))}**."

    # Common derivatives of x^n.
    if "derivada" in normalized:
        exponent_match = re.search(r"x\s*(?:\^|elevad[oa]\s+a(?:o)?\s*)\s*(-?\d+(?:[.,]\d+)?)", normalized)
        if exponent_match:
            exponent = float(exponent_match.group(1).replace(",", "."))
            new_exp = exponent - 1
            if exponent == 0:
                answer = "0"
            elif exponent == 1:
                answer = "1"
            elif exponent == 2:
                answer = "2x"
            else:
                answer = f"{_format_number(exponent)}x^{_format_number(new_exp)}"
            return f"A derivada de x^{_format_number(exponent)} é **{answer}**. Regra: desce o expoente e subtrai 1 dele."

        if any(phrase in normalized for phrase in ("o que e derivada", "sabe o que e derivada", "que e derivada")):
            return "Sei sim kkk. Derivada mede como uma função varia; geometricamente, é a inclinação da reta tangente naquele ponto."

    if any(phrase in normalized for phrase in ("quais contas", "que contas", "o que voce consegue calcular", "o que vc consegue calcular")):
        return "Faço aritmética, potência, raiz, porcentagem, regra de três, equações simples e derivadas básicas. Manda uma aí 😏"

    # Try a plain arithmetic expression embedded in a question.
    expr_match = re.search(r"(?:quanto(?: e| da)?|resultado(?: de)?|calcule|calcula)\s*[:=]?\s*([0-9\s+\-*/().,^×÷]+)", normalized)
    if expr_match:
        expression = expr_match.group(1).strip().rstrip("?.!")
        value = _safe_eval_expression(expression)
        if value is not None:
            return f"Dá **{_format_number(value)}**."

    if any(insult in normalized for insult in ("vai tomar no cu", "va tomar no cu", "seu cu", "fdp", "filho da puta")):
        if "derivada" in " ".join(row.get("content", "").casefold() for row in context[-8:]):
            return f"KKKKKK calma {author_name}, a derivada te estressou foi?"
        return f"KKKKKK qual foi {author_name} 💀"

    # Generic fallback only after trying to answer the actual content.
    if "?" in cleaned:
        options = [
            f"essa eu não peguei direito, manda de outro jeito {author_name}",
            f"explica melhor essa aí {author_name} kkkkk",
            f"essa ficou ambígua pra mim, reformula aí {author_name}",
        ]
    elif cleaned:
        options = [
            f"KKKKKK qual foi {author_name}",
            f"tô vendo isso aí {author_name} kkkkk",
            f"aí tu lançou essa e saiu correndo né {author_name} KKKK",
        ]
    else:
        options = [
            f"fala {author_name} kkkkk",
            f"qual foi {author_name} 💀",
            f"manda aí {author_name}",
        ]

    available = [item for item in options if item not in _recent_bot_replies]
    return random.choice(available or options)


async def _recent_context(message) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    used_chars = 0
    try:
        async for item in message.channel.history(limit=AUTO_REPLY_MAX_CONTEXT + 4):
            if item.id == message.id:
                continue
            text = (item.content or "").strip()
            if not text:
                continue
            text = text[:600]
            name = getattr(item.author, "display_name", None) or getattr(item.author, "name", "alguém")
            row_cost = len(str(name)) + len(text) + 4
            if rows and used_chars + row_cost > AUTO_REPLY_CONTEXT_CHARS:
                break
            rows.append({"name": str(name), "content": text})
            used_chars += row_cost
            if len(rows) >= AUTO_REPLY_MAX_CONTEXT:
                break
    except Exception:
        return []
    rows.reverse()
    return rows


async def _generate_reply(message, trigger: str | None) -> str:
    author_name = getattr(message.author, "display_name", None) or getattr(message.author, "name", "alguém")
    content = (message.content or "").strip()
    context = await _recent_context(message)
    long_term: list[dict[str, str]] = []
    long_term_provider = globals().get("_long_term_context_provider")
    if callable(long_term_provider):
        try:
            long_term = await long_term_provider(message)
        except Exception as exc:
            print(f"[Context] long-term retrieval failed: {type(exc).__name__}: {exc}", flush=True)
    profile = _reply_profile(content)
    print(
        f"[AutoReply] profile={profile['kind']} max_tokens={profile['max_tokens']} max_chars={profile['max_chars']}",
        flush=True,
    )

    if not base.GROQ_API_KEY:
        auto_reply_stats["smartFallbacks"] += 1
        return _smart_fallback_reply(message, trigger, context)

    context_text = "\n".join(
        f"[id={row.get('id', '?')}] {row['name']}: {row['content']}" for row in context
    )
    long_term_text = "\n".join(
        f"[id={row.get('id', '?')}] {row['name']}: {row['content']}" for row in long_term
    )
    recent_replies = "\n".join(f"- {item}" for item in _recent_bot_replies) or "(nenhuma)"
    user_prompt = (
        f"Servidor Discord: {getattr(getattr(message.channel, 'guild', None), 'name', '')}\n"
        f"Canal: {getattr(message.channel, 'name', str(message.channel.id))}\n"
        f"Gatilho detectado: {trigger or 'menção'}\n"
        f"Conversa recente detalhada do canal, do mais antigo para o mais novo:\n"
        f"{context_text or '(sem contexto recente)'}\n\n"
        f"Memórias relevantes recuperadas do HISTÓRICO INTEIRO do canal:\n"
        f"{long_term_text or '(nenhuma memória antiga relevante)'}\n\n"
        f"Respostas recentes que VOCÊ já deu e deve evitar repetir:\n{recent_replies}\n\n"
        f"Mensagem atual de {author_name}: {content or '(somente menção)'}"
    )

    payload = {
        "model": GROQ_AUTO_REPLY_MODEL,
        "temperature": 0.85,
        "max_completion_tokens": profile["max_tokens"],
        "messages": [
            {
                "role": "system",
                "content": (
                    "Você está conversando no Discord pela conta Greed. "
                    "Fale em português do Brasil de forma curta, espontânea e informal, como alguém do grupo. "
                    "A mensagem atual é o foco principal, mas entenda a conversa recente e as memórias relevantes recuperadas do histórico inteiro antes de responder. "
                    "Mensagens sem menção também fazem parte do contexto e podem explicar piadas, assunto, pessoas e continuidade. "
                    "Não fique preso em assunto antigo quando a conversa já mudou. Memória antiga é contexto, não obrigação de continuar o tema. "
                    "Se uma pessoa disser que não gostou de uma brincadeira, que algo machucou, ou pedir mudança no modo de falar, trate isso como preferência duradoura daquela mesma pessoa e não repita o padrão depois. "
                    "Se houver uma pergunta factual ou matemática, responda corretamente e diretamente antes de brincar. "
                    "Se a pessoa usar expressões como 'esse número', 'isso', 'agora multiplica' ou similares, resolva a referência usando a conversa recente. "
                    "Se for só uma menção sem assunto, pode perguntar o que a pessoa quer, mas varie a frase. "
                    "Nunca copie uma das respostas recentes listadas no prompt e evite bordões repetidos. "
                    "Pode usar risadas como kkk/KKKK e emoji ocasionalmente, sem exagerar. "
                    + (
                        "Quando o pedido envolver código: SEMPRE escreva o código em bloco Markdown cercado por três crases, informando a linguagem, preserve quebras de linha e indentação reais e nunca coloque código multilinha entre crases simples. Se a mensagem atual só mudar a linguagem, como 'quero em python', reutilize o pedido de código imediatamente anterior do histórico e apenas converta/adapte esse mesmo código; não troque por um exemplo genérico. Se pedirem para deixar maior, realmente expanda o programa. Você pode gerar resposta longa; o sistema divide automaticamente em várias mensagens do Discord. "
                        if profile["kind"] == "code"
                        else (
                            "Se a mensagem pedir explicação, resposta factual ou análise, responda com conteúdo suficiente, podendo usar vários parágrafos e listas. Não corte a resposta só para parecer curta. "
                            if profile["detailed"]
                            else "Em conversa casual, use normalmente 1 frase curta, no máximo 2. "
                        )
                    )
                    + "Nunca diga que é IA, bot, resposta automática, 'só código', 'código e bytes' ou equivalente. "
                    "Não invente fatos pessoais, não revele segredos/credenciais e não faça ameaças reais."
                ),
            },
            {"role": "user", "content": user_prompt},
        ],
    }

    try:
        async with httpx.AsyncClient(timeout=25) as http:
            response = await http.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={"Authorization": f"Bearer {base.GROQ_API_KEY}", "Content-Type": "application/json"},
                json=payload,
            )
        if response.is_error:
            detail = response.text.replace("\n", " ")[:500]
            raise RuntimeError(f"Groq HTTP {response.status_code}: {detail}")
        data = response.json()
        choice = data["choices"][0]
        finish_reason = str(choice.get("finish_reason") or "").lower()
        if finish_reason in {"length", "max_tokens"}:
            raise RuntimeError("Groq output token limit reached")
        reply = str(choice["message"]["content"]).strip()
        if not reply:
            raise RuntimeError("Groq returned an empty reply")
        if profile["kind"] == "code":
            fence_count = reply.count("\x60\x60\x60")
            if fence_count < 2 or fence_count % 2 != 0:
                raise RuntimeError("Groq returned incomplete code fence")
        reply = reply[: int(profile["max_chars"])]
        if reply in _recent_bot_replies:
            auto_reply_stats["smartFallbacks"] += 1
            return _smart_fallback_reply(message, trigger, context)
        return reply
    except Exception as exc:
        auto_reply_stats["groqFailures"] += 1
        auto_reply_stats["smartFallbacks"] += 1
        auto_reply_stats["lastModelError"] = f"{type(exc).__name__}: {exc}"[:700]
        print(f"[AutoReply] Groq fallback: {type(exc).__name__}: {exc}", flush=True)
        return _smart_fallback_reply(message, trigger, context)


def _split_discord_reply(text: str, limit: int = 1850) -> list[str]:
    """Split long Discord replies while keeping fenced code valid in every chunk."""
    text = (text or "").strip()
    if not text:
        return []
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    current = ""
    in_fence = False
    fence_lang = ""

    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        stripped = line.strip()
        is_fence = stripped.startswith("```")
        is_closing = bool(is_fence and in_fence)
        addition = line + "\n"
        reserve = 4 if in_fence and not is_closing else 0

        if current and len(current) + len(addition) + reserve > limit:
            if in_fence:
                # If this line is the model's closing fence, consume it by
                # closing this chunk rather than opening an empty next fence.
                chunks.append((current.rstrip() + "\n```").strip())
                if is_closing:
                    current = ""
                    in_fence = False
                    fence_lang = ""
                    continue
                current = f"```{fence_lang}\n"
            else:
                chunks.append(current.rstrip())
                current = ""

        # Extremely long single lines are split as a last resort.
        while len(current) + len(addition) + (4 if in_fence and not is_closing else 0) > limit:
            available = max(200, limit - len(current) - (4 if in_fence else 0))
            piece = addition[:available]
            addition = addition[available:]
            current += piece
            if in_fence:
                chunks.append((current.rstrip() + "\n```").strip())
                current = f"```{fence_lang}\n"
            else:
                chunks.append(current.rstrip())
                current = ""

        current += addition

        if is_fence:
            if in_fence:
                in_fence = False
                fence_lang = ""
            else:
                in_fence = True
                fence_lang = stripped[3:].strip()

    if current.strip():
        if in_fence:
            current = current.rstrip() + "\n```"
        chunks.append(current.strip())

    return [chunk for chunk in chunks if chunk]


@client.event
async def on_message(message) -> None:
    if not AUTO_REPLY_ENABLED or client.user is None:
        return

    author = getattr(message, "author", None)
    if author is None:
        return

    is_self = getattr(author, "id", None) == getattr(client.user, "id", None)
    if is_self:
        if not AUTO_REPLY_ALLOW_SELF:
            return
        # Messages generated by this auto-reply are also "self" messages.
        # Ignore them so manual self-triggers work without reply loops.
        current_text = (message.content or "").strip()
        if current_text and current_text in _recent_bot_replies:
            return
    elif bool(getattr(author, "bot", False)):
        return

    guild = getattr(message, "guild", None) or getattr(getattr(message, "channel", None), "guild", None)
    if guild is None or str(getattr(guild, "id", "")) != AUTO_REPLY_GUILD_ID:
        return

    channel_id = str(getattr(getattr(message, "channel", None), "id", ""))
    if AUTO_REPLY_CHANNEL_ID and channel_id != AUTO_REPLY_CHANNEL_ID:
        auto_reply_stats["skippedChannel"] += 1
        return

    matched, trigger = _trigger_match(message)
    if not matched:
        return

    normalized_message = _normalize(_clean_message_text(message))
    duplicate_key = (int(message.channel.id), int(author.id), normalized_message)
    now = time.monotonic()
    previous_seen = _last_seen_message.get(duplicate_key, 0.0)
    if normalized_message and AUTO_REPLY_DUPLICATE_WINDOW_SECONDS and now - previous_seen < AUTO_REPLY_DUPLICATE_WINDOW_SECONDS:
        auto_reply_stats["skippedDuplicate"] += 1
        return
    _last_seen_message[duplicate_key] = now
    if len(_last_seen_message) > 500:
        cutoff = now - max(60.0, AUTO_REPLY_DUPLICATE_WINDOW_SECONDS * 4)
        for key, seen_at in list(_last_seen_message.items()):
            if seen_at < cutoff:
                _last_seen_message.pop(key, None)

    auto_reply_stats["matched"] += 1
    auto_reply_stats["lastTrigger"] = {
        "messageId": str(message.id),
        "channelId": channel_id,
        "authorId": str(author.id),
        "trigger": trigger,
    }

    key = (int(message.channel.id), int(author.id))
    if AUTO_REPLY_COOLDOWN_SECONDS and now - _last_reply_at[key] < AUTO_REPLY_COOLDOWN_SECONDS:
        auto_reply_stats["skippedCooldown"] += 1
        return
    _last_reply_at[key] = now

    await asyncio.sleep(random.uniform(0.35, 0.95))

    try:
        reply = await _generate_reply(message, trigger)
        chunks = _split_discord_reply(reply)
        if not chunks:
            raise RuntimeError("empty reply after Discord splitting")

        sent = None
        for index, chunk in enumerate(chunks):
            _recent_bot_replies.append(chunk)
            if index == 0:
                sent = await message.channel.send(chunk, reference=message, mention_author=False)
            else:
                await message.channel.send(chunk)
                await asyncio.sleep(0.15)

        auto_reply_stats["replied"] += 1
        auto_reply_stats["lastReplyAt"] = time.time()
        auto_reply_stats["lastReplyChunks"] = len(chunks)
        print(
            f"[AutoReply] replied guild={guild.id} channel={message.channel.id} author={author.id} trigger={trigger} replyId={sent.id if sent else 'none'} chunks={len(chunks)}",
            flush=True,
        )
    except Exception as exc:
        print(f"[AutoReply] failed: {type(exc).__name__}: {exc}", flush=True)


@app.get("/api/auto-reply/status", dependencies=[Depends(base.require_api_token)])
async def auto_reply_status() -> dict[str, Any]:
    return {
        "enabled": AUTO_REPLY_ENABLED,
        "guildId": AUTO_REPLY_GUILD_ID,
        "channelId": AUTO_REPLY_CHANNEL_ID,
        "triggers": AUTO_REPLY_TRIGGERS,
        "mentionTrigger": True,
        "allowSelfTrigger": AUTO_REPLY_ALLOW_SELF,
        "cooldownSeconds": AUTO_REPLY_COOLDOWN_SECONDS,
        "duplicateWindowSeconds": AUTO_REPLY_DUPLICATE_WINDOW_SECONDS,
        "maxContextMessages": AUTO_REPLY_MAX_CONTEXT,
        "contextCharBudget": AUTO_REPLY_CONTEXT_CHARS,
        "casualMaxChars": AUTO_REPLY_MAX_CHARS,
        "detailMaxChars": AUTO_REPLY_DETAIL_MAX_CHARS,
        "codeMaxChars": AUTO_REPLY_CODE_MAX_CHARS,
        "discordChunkChars": 1850,
        "model": GROQ_AUTO_REPLY_MODEL if base.GROQ_API_KEY else None,
        "groqConfigured": bool(base.GROQ_API_KEY),
        "recentReplies": list(_recent_bot_replies),
        "stats": auto_reply_stats,
    }
