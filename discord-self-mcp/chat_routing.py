"""Detecção robusta de menções, respostas diretas e conversas recentes no Discord."""
import re


def directly_mentions(message, user_id: int) -> bool:
    if any(getattr(user, "id", None) == user_id for user in (getattr(message, "mentions", None) or [])):
        return True
    # Algumas mensagens chegam sem a lista de menções resolvida.
    return bool(re.search(rf"<@!?{int(user_id)}>", str(getattr(message, "content", "") or "")))


async def replies_to_self(message, user_id: int) -> bool:
    reference = getattr(message, "reference", None)
    if reference is None:
        return False
    resolved = getattr(reference, "resolved", None) or getattr(reference, "cached_message", None)
    author = getattr(resolved, "author", None)
    if author is not None:
        return getattr(author, "id", None) == user_id
    message_id = getattr(reference, "message_id", None)
    if not message_id:
        return False
    channel = getattr(message, "channel", None)
    fetch = getattr(channel, "fetch_message", None)
    if not callable(fetch):
        return False
    try:
        replied = await fetch(message_id)
        return getattr(getattr(replied, "author", None), "id", None) == user_id
    except Exception:
        # A falha ao obter um post excluído ou não autorizado não bloqueia o chat.
        return False


def session_key(message) -> tuple[int, int, int]:
    guild = getattr(message, "guild", None) or getattr(getattr(message, "channel", None), "guild", None)
    return (
        int(getattr(guild, "id", 0)),
        int(getattr(getattr(message, "channel", None), "id", 0)),
        int(getattr(getattr(message, "author", None), "id", 0)),
    )


def active_followup(message, sessions: dict, now: float) -> bool:
    content = str(getattr(message, "content", "") or "").strip()
    if not content or content.startswith(("!", "/", ".")):
        return False
    key = session_key(message)
    deadline = sessions.get(key, 0.0)
    if deadline and now >= deadline:
        sessions.pop(key, None)
        return False
    return bool(deadline and now < deadline)
