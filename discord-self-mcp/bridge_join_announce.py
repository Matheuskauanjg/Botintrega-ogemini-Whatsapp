import os
from typing import Any

import bridge_auto as auto

app = auto.app
voice = auto.stack.music.voice

VOICE_JOIN_ANNOUNCEMENT = os.getenv(
    "VOICE_JOIN_ANNOUNCEMENT",
    "Bot Greed entrando. Tudo que for falado aqui pode ser ouvido e processado.",
).strip()
VOICE_JOIN_ANNOUNCEMENT_ENABLED = os.getenv(
    "VOICE_JOIN_ANNOUNCEMENT_ENABLED", "true"
).strip().lower() in {"1", "true", "yes", "on"}

_original_handle_voice_send = voice.handle_voice_send


async def handle_voice_send_with_announcement(body: Any) -> dict[str, Any]:
    target = body.to[len(voice.VOICE_PREFIX):].strip() if body.to.casefold().startswith(voice.VOICE_PREFIX) else ""
    folded = body.message.strip().casefold()
    is_join = folded in {"__join__", "/join", "entrar", "!join"}

    result = await _original_handle_voice_send(body)

    if is_join and target and VOICE_JOIN_ANNOUNCEMENT_ENABLED and VOICE_JOIN_ANNOUNCEMENT:
        try:
            spoken = await voice.speak_robot(target, VOICE_JOIN_ANNOUNCEMENT)
            result["announcement"] = {
                "spoken": bool(spoken.get("voice", {}).get("spoken")),
                "text": VOICE_JOIN_ANNOUNCEMENT,
            }
        except Exception as exc:
            result["announcement"] = {
                "spoken": False,
                "text": VOICE_JOIN_ANNOUNCEMENT,
                "error": f"{type(exc).__name__}: {exc}",
            }

    return result


voice.handle_voice_send = handle_voice_send_with_announcement
