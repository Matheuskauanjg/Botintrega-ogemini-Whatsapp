from typing import Any

import bridge_gprefix as stack

app = stack.app
fun = stack.fun
auto = fun.auto
base = auto.base

# Save the original Groq handlers before bridge_gemini replaces them.
_original_auto_generate = auto._generate_reply
_original_ask_groq = fun._ask_groq

import bridge_gemini as gemini  # noqa: E402


async def _auto_reply_chain(message: Any, trigger: str | None) -> str:
    """Provider order: Groq -> Gemini -> local smart fallback."""
    context = await auto._recent_context(message)

    if base.GROQ_API_KEY:
        failures_before = int(auto.auto_reply_stats.get("groqFailures", 0))
        reply = await _original_auto_generate(message, trigger)
        failures_after = int(auto.auto_reply_stats.get("groqFailures", 0))
        if failures_after == failures_before:
            auto.auto_reply_stats["provider"] = "groq"
            return reply
        print("[AutoReply] Groq falhou; tentando Gemini", flush=True)

    # bridge_gemini already performs Gemini -> local fallback.
    reply = await gemini._generate_auto_reply(message, trigger)
    provider = "gemini" if gemini.GEMINI_API_KEY else "local"
    auto.auto_reply_stats["provider"] = provider
    return reply


async def _ask_chain(prompt: str) -> str:
    """Provider order for !g ask: Groq -> Gemini."""
    if base.GROQ_API_KEY:
        try:
            return await _original_ask_groq(prompt)
        except Exception as exc:
            print(f"[GCommand] Groq falhou; tentando Gemini: {type(exc).__name__}", flush=True)

    return await gemini._ask_gemini(prompt)


auto._generate_reply = _auto_reply_chain
fun._ask_groq = _ask_chain

auto.auto_reply_stats["providerOrder"] = ["groq", "gemini", "local"]
auto.auto_reply_stats["geminiConfigured"] = bool(gemini.GEMINI_API_KEY)
auto.auto_reply_stats["geminiModel"] = gemini.GEMINI_MODEL
