import os
from typing import Any

import httpx

import bridge_gprefix as stack

app = stack.app
fun = stack.fun
auto = fun.auto

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite").strip()
GEMINI_API_BASE = os.getenv("GEMINI_API_BASE", "https://generativelanguage.googleapis.com/v1beta").rstrip("/")


async def _gemini_text(prompt: str, system: str, max_output_tokens: int = 220) -> str:
    if not GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY não configurada")

    url = f"{GEMINI_API_BASE}/models/{GEMINI_MODEL}:generateContent"
    payload = {
        "contents": [
            {
                "role": "user",
                "parts": [
                    {
                        "text": f"INSTRUÇÕES:\n{system}\n\nCONTEÚDO:\n{prompt}"
                    }
                ],
            }
        ],
        "generationConfig": {
            "maxOutputTokens": max_output_tokens,
        },
    }

    async with httpx.AsyncClient(timeout=30) as http:
        response = await http.post(
            url,
            headers={
                "x-goog-api-key": GEMINI_API_KEY,
                "Content-Type": "application/json",
            },
            json=payload,
        )

    if response.is_error:
        # Do not leak the API key or full provider response into Discord/logs.
        raise RuntimeError(f"Gemini HTTP {response.status_code}")

    data = response.json()
    candidates = data.get("candidates") or []
    if not candidates:
        raise RuntimeError("Gemini sem candidato de resposta")

    parts = (((candidates[0] or {}).get("content") or {}).get("parts") or [])
    text = " ".join(str(part.get("text", "")).strip() for part in parts if part.get("text")).strip()
    if not text:
        raise RuntimeError("Gemini retornou resposta vazia")
    return " ".join(text.split())


async def _generate_auto_reply(message: Any, trigger: str | None) -> str:
    context = await auto._recent_context(message)
    if not GEMINI_API_KEY:
        auto.auto_reply_stats["smartFallbacks"] += 1
        return auto._smart_fallback_reply(message, trigger, context)

    author_name = getattr(message.author, "display_name", None) or getattr(message.author, "name", "alguém")
    content = (message.content or "").strip()
    context_text = "\n".join(f"{row['name']}: {row['content']}" for row in context)
    recent_replies = "\n".join(f"- {item}" for item in auto._recent_bot_replies) or "(nenhuma)"

    prompt = (
        f"Servidor Discord: {getattr(getattr(message.channel, 'guild', None), 'name', '')}\n"
        f"Canal: {getattr(message.channel, 'name', str(message.channel.id))}\n"
        f"Gatilho detectado: {trigger or 'menção'}\n"
        f"Conversa recente do canal, do mais antigo para o mais novo:\n{context_text or '(sem contexto)'}\n\n"
        f"Respostas recentes que você já deu e deve evitar repetir:\n{recent_replies}\n\n"
        f"Mensagem atual de {author_name}: {content or '(somente menção)'}"
    )
    system = (
        "Você está conversando no Discord pela conta Greed. "
        "Responda em português do Brasil de forma curta, espontânea e informal, como alguém do grupo. "
        "Entenda toda a conversa recente antes de responder e use referências como 'esse número', 'isso' e 'agora multiplica'. "
        "Se houver pergunta factual ou matemática, responda corretamente e diretamente antes de brincar. "
        "Não repita bordões ou respostas recentes. Pode usar kkk/KKKK e emoji ocasionalmente, sem exagerar. "
        "Use normalmente uma frase curta, no máximo duas. "
        "Não diga que é IA, bot ou resposta automática. "
        "Não invente fatos pessoais, não revele credenciais/tokens/segredos e não faça ameaças reais."
    )

    try:
        reply = (await _gemini_text(prompt, system, 220))[: auto.AUTO_REPLY_MAX_CHARS]
        if reply in auto._recent_bot_replies:
            auto.auto_reply_stats["smartFallbacks"] += 1
            return auto._smart_fallback_reply(message, trigger, context)
        auto.auto_reply_stats["geminiReplies"] = int(auto.auto_reply_stats.get("geminiReplies", 0)) + 1
        auto.auto_reply_stats["lastModelError"] = None
        return reply
    except Exception as exc:
        auto.auto_reply_stats["geminiFailures"] = int(auto.auto_reply_stats.get("geminiFailures", 0)) + 1
        auto.auto_reply_stats["smartFallbacks"] += 1
        auto.auto_reply_stats["lastModelError"] = f"{type(exc).__name__}: {exc}"[:700]
        print(f"[AutoReply] Gemini fallback: {type(exc).__name__}: {exc}", flush=True)
        return auto._smart_fallback_reply(message, trigger, context)


async def _ask_gemini(prompt: str) -> str:
    system = (
        "Responda em português do Brasil para ser falado em uma call do Discord. "
        "Seja direto, natural e curto: normalmente uma ou duas frases. "
        "Se for matemática ou pergunta factual, responda corretamente antes de qualquer brincadeira. "
        "Não revele credenciais, tokens ou segredos e não faça ameaças reais."
    )
    try:
        return (await _gemini_text(prompt[:1800], system, 220))[:700]
    except Exception as exc:
        print(f"[GCommand] Gemini ask fallback: {type(exc).__name__}: {exc}", flush=True)
        return "A IA ficou indisponível agora, tenta de novo em alguns segundos."


# bridge_auto.on_message resolves this global at runtime.
auto._generate_reply = _generate_auto_reply

# bridge_fun's /ask handler resolves _ask_groq from its module at runtime.
# Keep the old symbol name so no command-routing code needs to change.
fun._ask_groq = _ask_gemini

# Expose provider/model in process-level diagnostics without changing existing routes.
auto.auto_reply_stats["provider"] = "gemini"
auto.auto_reply_stats["geminiModel"] = GEMINI_MODEL
