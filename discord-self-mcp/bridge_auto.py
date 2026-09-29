import asyncio
import os
import random
import re
import time
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
AUTO_REPLY_TRIGGERS = [
    item.casefold().strip()
    for item in os.getenv("AUTO_REPLY_TRIGGERS", "grade,greed").split(",")
    if item.strip()
]
AUTO_REPLY_COOLDOWN_SECONDS = max(0.0, float(os.getenv("AUTO_REPLY_COOLDOWN_SECONDS", "5")))
AUTO_REPLY_MAX_CONTEXT = max(1, min(15, int(os.getenv("AUTO_REPLY_MAX_CONTEXT", "8"))))
AUTO_REPLY_MAX_CHARS = max(50, min(1500, int(os.getenv("AUTO_REPLY_MAX_CHARS", "450"))))
GROQ_AUTO_REPLY_MODEL = os.getenv("GROQ_AUTO_REPLY_MODEL", "openai/gpt-oss-120b").strip()

_last_reply_at: dict[tuple[int, int], float] = defaultdict(float)
_recent_bot_replies: deque[str] = deque(maxlen=12)
auto_reply_stats: dict[str, Any] = {
    "matched": 0,
    "replied": 0,
    "skippedCooldown": 0,
    "groqFailures": 0,
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


def _fallback_reply(message, trigger: str | None) -> str:
    author_name = getattr(message.author, "display_name", None) or getattr(message.author, "name", "mano")
    content = (message.content or "").strip()
    cleaned = content
    if client.user is not None:
        cleaned = re.sub(rf"<@!?{client.user.id}>", "", cleaned).strip()
    for item in AUTO_REPLY_TRIGGERS:
        cleaned = re.sub(rf"(?<!\w){re.escape(item)}(?!\w)", "", cleaned, flags=re.IGNORECASE).strip()

    if "?" in cleaned:
        options = [
            f"aí tu me quebra {author_name} kkkkk",
            f"pergunta forte essa aí {author_name} 💀",
            f"depende, qual é tua teoria {author_name}?",
            f"do nada essa pergunta {author_name} KKKKK",
        ]
    elif cleaned:
        options = [
            f"KKKKKK qual foi {author_name}",
            f"tô vendo isso aí {author_name} kkkkk",
            f"fala mais {author_name}, agora fiquei curioso",
            f"aí tu lançou essa e saiu correndo né {author_name} KKKK",
        ]
    else:
        options = [
            f"fala {author_name} kkkkk",
            f"qual foi {author_name} 💀",
            f"manda aí {author_name}",
            f"tô aqui {author_name}, desembucha kkkkk",
            f"chamou de novo {author_name}? KKKK",
        ]

    available = [item for item in options if item not in _recent_bot_replies]
    return random.choice(available or options)


async def _recent_context(message) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    try:
        async for item in message.channel.history(limit=AUTO_REPLY_MAX_CONTEXT + 2):
            if item.id == message.id:
                continue
            text = (item.content or "").strip()
            if not text:
                continue
            name = getattr(item.author, "display_name", None) or getattr(item.author, "name", "alguém")
            rows.append({"name": str(name), "content": text[:600]})
            if len(rows) >= AUTO_REPLY_MAX_CONTEXT:
                break
    except Exception:
        return []
    rows.reverse()
    return rows


async def _generate_reply(message, trigger: str | None) -> str:
    author_name = getattr(message.author, "display_name", None) or getattr(message.author, "name", "alguém")
    content = (message.content or "").strip()

    if not base.GROQ_API_KEY:
        return _fallback_reply(message, trigger)

    context = await _recent_context(message)
    context_text = "\n".join(f"{row['name']}: {row['content']}" for row in context)
    recent_replies = "\n".join(f"- {item}" for item in _recent_bot_replies) or "(nenhuma)"
    user_prompt = (
        f"Servidor Discord: {getattr(getattr(message.channel, 'guild', None), 'name', '')}\n"
        f"Canal: {getattr(message.channel, 'name', str(message.channel.id))}\n"
        f"Gatilho detectado: {trigger or 'menção'}\n"
        f"Contexto recente, do mais antigo para o mais novo:\n{context_text or '(sem contexto)'}\n\n"
        f"Respostas recentes que VOCÊ já deu e deve evitar repetir:\n{recent_replies}\n\n"
        f"Mensagem atual de {author_name}: {content or '(somente menção)'}"
    )

    payload = {
        "model": GROQ_AUTO_REPLY_MODEL,
        "temperature": 1.0,
        "max_completion_tokens": 180,
        "messages": [
            {
                "role": "system",
                "content": (
                    "Você está conversando no Discord pela conta Greed. "
                    "Fale em português do Brasil de forma curta, espontânea e informal, como alguém do grupo. "
                    "A mensagem atual é o foco principal: responda ao que a pessoa realmente perguntou ou falou. "
                    "Se houver uma pergunta, responda a pergunta; não responda apenas 'chamou?'. "
                    "Se for só uma menção sem assunto, pode perguntar o que a pessoa quer, mas varie a frase. "
                    "Use o histórico recente para entender piadas e continuidade da conversa. "
                    "Nunca copie uma das respostas recentes listadas no prompt e evite bordões repetidos. "
                    "Pode usar risadas como kkk/KKKK e emoji ocasionalmente, sem exagerar. "
                    "Use normalmente 1 frase curta, no máximo 2. "
                    "Não diga que é IA, bot ou resposta automática. "
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
        reply = str(data["choices"][0]["message"]["content"]).strip()
        if not reply:
            raise RuntimeError("Groq returned an empty reply")
        reply = reply[:AUTO_REPLY_MAX_CHARS]
        if reply in _recent_bot_replies:
            return _fallback_reply(message, trigger)
        return reply
    except Exception as exc:
        auto_reply_stats["groqFailures"] += 1
        auto_reply_stats["lastModelError"] = f"{type(exc).__name__}: {exc}"[:700]
        print(f"[AutoReply] Groq fallback: {type(exc).__name__}: {exc}", flush=True)
        return _fallback_reply(message, trigger)


@client.event
async def on_message(message) -> None:
    if not AUTO_REPLY_ENABLED or client.user is None:
        return

    author = getattr(message, "author", None)
    if author is None or getattr(author, "id", None) == getattr(client.user, "id", None):
        return
    if bool(getattr(author, "bot", False)):
        return

    guild = getattr(message, "guild", None) or getattr(getattr(message, "channel", None), "guild", None)
    if guild is None or str(getattr(guild, "id", "")) != AUTO_REPLY_GUILD_ID:
        return

    matched, trigger = _trigger_match(message)
    if not matched:
        return

    auto_reply_stats["matched"] += 1
    auto_reply_stats["lastTrigger"] = {
        "messageId": str(message.id),
        "channelId": str(message.channel.id),
        "authorId": str(author.id),
        "trigger": trigger,
    }

    key = (int(message.channel.id), int(author.id))
    now = time.monotonic()
    if AUTO_REPLY_COOLDOWN_SECONDS and now - _last_reply_at[key] < AUTO_REPLY_COOLDOWN_SECONDS:
        auto_reply_stats["skippedCooldown"] += 1
        return
    _last_reply_at[key] = now

    await asyncio.sleep(random.uniform(0.35, 0.95))

    try:
        reply = await _generate_reply(message, trigger)
        sent = await message.channel.send(reply, reference=message, mention_author=False)
        _recent_bot_replies.append(reply)
        auto_reply_stats["replied"] += 1
        auto_reply_stats["lastReplyAt"] = time.time()
        auto_reply_stats["lastModelError"] = None
        print(
            f"[AutoReply] replied guild={guild.id} channel={message.channel.id} author={author.id} trigger={trigger} replyId={sent.id}",
            flush=True,
        )
    except Exception as exc:
        print(f"[AutoReply] failed: {type(exc).__name__}: {exc}", flush=True)


@app.get("/api/auto-reply/status", dependencies=[Depends(base.require_api_token)])
async def auto_reply_status() -> dict[str, Any]:
    return {
        "enabled": AUTO_REPLY_ENABLED,
        "guildId": AUTO_REPLY_GUILD_ID,
        "triggers": AUTO_REPLY_TRIGGERS,
        "mentionTrigger": True,
        "cooldownSeconds": AUTO_REPLY_COOLDOWN_SECONDS,
        "model": GROQ_AUTO_REPLY_MODEL if base.GROQ_API_KEY else None,
        "groqConfigured": bool(base.GROQ_API_KEY),
        "recentReplies": list(_recent_bot_replies),
        "stats": auto_reply_stats,
    }
