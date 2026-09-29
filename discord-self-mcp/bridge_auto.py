import asyncio
import os
import random
import re
import time
from collections import defaultdict
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
GROQ_AUTO_REPLY_MODEL = os.getenv("GROQ_AUTO_REPLY_MODEL", "llama-3.3-70b-versatile").strip()

_last_reply_at: dict[tuple[int, int], float] = defaultdict(float)
auto_reply_stats: dict[str, Any] = {
    "matched": 0,
    "replied": 0,
    "skippedCooldown": 0,
    "groqFailures": 0,
    "lastTrigger": None,
    "lastReplyAt": None,
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


async def _recent_context(message) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    try:
        async for item in message.channel.history(limit=AUTO_REPLY_MAX_CONTEXT + 1):
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
        return f"fala {author_name}, chamou?"

    context = await _recent_context(message)
    context_text = "\n".join(f"{row['name']}: {row['content']}" for row in context)
    user_prompt = (
        f"Servidor Discord: {getattr(getattr(message.channel, 'guild', None), 'name', '')}\n"
        f"Canal: {getattr(message.channel, 'name', str(message.channel.id))}\n"
        f"Gatilho: {trigger or 'menção'}\n"
        f"Contexto recente:\n{context_text or '(sem contexto)'}\n\n"
        f"Mensagem atual de {author_name}: {content or '(somente menção)'}"
    )

    payload = {
        "model": GROQ_AUTO_REPLY_MODEL,
        "temperature": 0.9,
        "max_completion_tokens": 160,
        "messages": [
            {
                "role": "system",
                "content": (
                    "Você responde automaticamente no Discord pela conta Greed. "
                    "Responda em português do Brasil, curto, natural e informal, como conversa de Discord. "
                    "Use no máximo 2 frases e não diga que é IA, bot ou resposta automática. "
                    "Responda ao conteúdo atual usando o contexto somente quando ele ajudar. "
                    "Não invente fatos, não revele segredos ou credenciais e não continue discussões perigosas."
                ),
            },
            {"role": "user", "content": user_prompt},
        ],
    }

    try:
        async with httpx.AsyncClient(timeout=20) as http:
            response = await http.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={"Authorization": f"Bearer {base.GROQ_API_KEY}", "Content-Type": "application/json"},
                json=payload,
            )
        if response.is_error:
            raise RuntimeError(f"Groq HTTP {response.status_code}")
        data = response.json()
        reply = str(data["choices"][0]["message"]["content"]).strip()
        if not reply:
            raise RuntimeError("Groq returned an empty reply")
        return reply[:AUTO_REPLY_MAX_CHARS]
    except Exception as exc:
        auto_reply_stats["groqFailures"] += 1
        print(f"[AutoReply] Groq fallback: {type(exc).__name__}: {exc}", flush=True)
        return f"fala {author_name}, chamou?"


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

    # Slight human-like delay and one reply per triggering message.
    await asyncio.sleep(random.uniform(0.35, 0.95))

    try:
        reply = await _generate_reply(message, trigger)
        await message.channel.send(reply, reference=message, mention_author=False)
        auto_reply_stats["replied"] += 1
        auto_reply_stats["lastReplyAt"] = time.time()
        print(
            f"[AutoReply] replied guild={guild.id} channel={message.channel.id} author={author.id} trigger={trigger}",
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
        "stats": auto_reply_stats,
    }
