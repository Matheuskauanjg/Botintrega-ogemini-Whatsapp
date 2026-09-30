import re
from typing import Any

import bridge_fun as fun

app = fun.app
voice = fun.voice
base = fun.base

PREFIX = "!g"


def _replace_help_prefix(value: str) -> str:
    """Convert documented /commands to the public !g command syntax."""
    return re.sub(r"(?<!\S)/([^\s|]+)", r"!g \1", value)


def _rewrite_help(result: dict[str, Any]) -> dict[str, Any]:
    commands = result.get("commands")
    if isinstance(commands, dict):
        result["commands"] = {
            _replace_help_prefix(str(key)): value
            for key, value in commands.items()
        }
    result["prefix"] = PREFIX
    result["usage"] = "!g <comando>"
    return result


def _copy_body_with_message(body: Any, message: str) -> Any:
    if hasattr(body, "model_copy"):
        return body.model_copy(update={"message": message})
    if hasattr(body, "copy"):
        return body.copy(update={"message": message})
    body.message = message
    return body


# Capture the currently active /api/send endpoint from bridge_fun, then replace it
# with a thin prefix adapter. This keeps all existing command implementations intact.
_previous_send_handler = None
for route in list(app.router.routes):
    if getattr(route, "path", None) == "/api/send" and "POST" in (getattr(route, "methods", set()) or set()):
        _previous_send_handler = getattr(route, "endpoint", None)
        app.router.routes.remove(route)

if _previous_send_handler is None:
    raise RuntimeError("Unable to locate existing POST /api/send handler")


@app.post("/api/send", dependencies=[voice.Depends(base.require_api_token)])
async def send_message_with_g_prefix(body: Any) -> dict[str, Any]:
    is_voice = str(body.to).casefold().startswith(voice.VOICE_PREFIX)
    if not is_voice:
        return await _previous_send_handler(body)

    message = str(body.message or "").strip()
    folded = message.casefold()

    # !g by itself behaves like !g help.
    if folded == PREFIX:
        message = f"{PREFIX} help"
        folded = message.casefold()

    if folded.startswith(f"{PREFIX} "):
        command_text = message[len(PREFIX):].strip()
        if not command_text:
            command_text = "help"

        # Existing handlers use slash internally; only the public syntax changes.
        translated = "/" + command_text
        translated_body = _copy_body_with_message(body, translated)
        try:
            result = await fun.handle_voice_send_with_fun(translated_body)
        except voice.HTTPException as exc:
            detail = str(getattr(exc, "detail", exc))
            detail = _replace_help_prefix(detail)
            raise voice.HTTPException(status_code=getattr(exc, "status_code", 400), detail=detail) from exc

        if command_text.casefold() in {"help", "ajuda"}:
            return _rewrite_help(result)
        return result

    # Commands no longer use /cmd or !cmd. Plain speech still goes through to TTS.
    if message.startswith("/") or message.startswith("!"):
        raise voice.HTTPException(
            status_code=400,
            detail="Os comandos agora usam !g. Exemplo: !g help, !g coin, !g sound airhorn",
        )

    return await _previous_send_handler(body)
