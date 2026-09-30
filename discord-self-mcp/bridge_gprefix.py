import os
import re
from typing import Any

import bridge_fun as fun

app = fun.app
voice = fun.voice
base = fun.base
client = fun.client

PREFIX = "!g"
COMMAND_GUILD_ID = os.getenv(
    "G_COMMAND_GUILD_ID",
    os.getenv("AUTO_REPLY_GUILD_ID", "1251266361569710222"),
).strip()
DEFAULT_VOICE_CHANNEL_ID = os.getenv("DEFAULT_VOICE_CHANNEL_ID", "1530374141625106522").strip()


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


def _voice_target_for_message(message: Any) -> str:
    """Prefer the owner's current call, then an already-active call, then the configured default."""
    author = getattr(message, "author", None)
    voice_state = getattr(author, "voice", None)
    author_channel = getattr(voice_state, "channel", None) if voice_state else None
    author_channel_id = getattr(author_channel, "id", None)
    if author_channel_id:
        return str(author_channel_id)

    vc = getattr(voice, "active_voice_client", None)
    active_channel = getattr(vc, "channel", None) if vc else None
    active_id = getattr(active_channel, "id", None)
    if active_id:
        return str(active_id)

    return DEFAULT_VOICE_CHANNEL_ID


def _help_text(result: dict[str, Any]) -> str:
    commands = result.get("commands") or {}
    names: list[str] = []
    for key in commands.keys():
        public = _replace_help_prefix(str(key)).split("|")[0].strip()
        if public and public not in names:
            names.append(public)
    if not names:
        return "Use `!g help` para ver os comandos."
    return "**Comandos Greed:**\n" + " • ".join(names)[:1800]


def _feedback_text(command_text: str, result: dict[str, Any], target: str) -> str:
    command = (result.get("command") or command_text.split(maxsplit=1)[0] or "comando").strip()
    text = result.get("text")
    if text:
        return str(text)[:1700]

    if command == "coin":
        value = result.get("result")
        return f"🪙 Deu **{value}**. Áudio enviado para a call `{target}`." if value else f"🪙 Moeda jogada. Call `{target}`."
    if command == "dice":
        value = result.get("result")
        sides = result.get("sides")
        return f"🎲 d{sides}: **{value}**. Áudio enviado para a call `{target}`."
    if command == "choose":
        return f"🎯 Escolhi **{result.get('chosen')}**. Áudio enviado para a call `{target}`."
    if command == "8ball":
        return f"🎱 {result.get('answer', 'Respondido')}. Call `{target}`."
    if command in {"say", "repeat", "mock", "countdown", "ask"}:
        return f"🔊 `{PREFIX} {command}` executado na call `{target}`."
    if command in {"sound", "randomsound"} or result.get("mode") == "soundboard":
        sound = result.get("sound") or "efeito"
        return f"🔊 Som **{sound}** tocado na call `{target}`."
    if command in {"play", "search", "pause", "resume", "skip", "stop", "queue", "nowplaying"}:
        return f"🎵 `{PREFIX} {command}` executado na call `{target}`."
    if command in {"join", "leave"}:
        return f"🎙️ `{PREFIX} {command}` executado para a call `{target}`."
    return f"✅ `{PREFIX} {command_text}` executado. Call `{target}`."


async def _execute_prefixed_voice_command(body: Any) -> dict[str, Any]:
    message = str(body.message or "").strip()
    folded = message.casefold()

    if folded == PREFIX:
        message = f"{PREFIX} help"
        folded = message.casefold()

    if not folded.startswith(f"{PREFIX} "):
        raise voice.HTTPException(status_code=400, detail="Use !g <comando>. Exemplo: !g help")

    command_text = message[len(PREFIX):].strip() or "help"
    translated = "/" + command_text
    translated_body = _copy_body_with_message(body, translated)
    try:
        result = await fun.handle_voice_send_with_fun(translated_body)
    except voice.HTTPException as exc:
        detail = _replace_help_prefix(str(getattr(exc, "detail", exc)))
        raise voice.HTTPException(status_code=getattr(exc, "status_code", 400), detail=detail) from exc

    if command_text.casefold() in {"help", "ajuda"}:
        return _rewrite_help(result)
    return result


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
    if message.casefold() == PREFIX or message.casefold().startswith(f"{PREFIX} "):
        return await _execute_prefixed_voice_command(body)

    if message.startswith("/") or message.startswith("!"):
        raise voice.HTTPException(
            status_code=400,
            detail="Os comandos agora usam !g. Exemplo: !g help, !g coin, !g sound airhorn",
        )

    return await _previous_send_handler(body)


_previous_on_message = client.on_message


@client.event
async def on_message(message: Any) -> None:
    content = str(getattr(message, "content", "") or "").strip()
    folded = content.casefold()
    is_g_command = folded == PREFIX or folded.startswith(f"{PREFIX} ")

    if not is_g_command:
        await _previous_on_message(message)
        return

    me = client.user
    author = getattr(message, "author", None)
    if me is None or author is None or getattr(author, "id", None) != getattr(me, "id", None):
        return

    guild = getattr(message, "guild", None) or getattr(getattr(message, "channel", None), "guild", None)
    if COMMAND_GUILD_ID and (guild is None or str(getattr(guild, "id", "")) != COMMAND_GUILD_ID):
        return

    target = _voice_target_for_message(message)
    body = base.SendMessageBody(to=f"voice:{target}", message=content)

    try:
        result = await _execute_prefixed_voice_command(body)
        command_text = content[len(PREFIX):].strip().casefold() if len(content) > len(PREFIX) else "help"

        if command_text in {"", "help", "ajuda"}:
            reply_text = _help_text(result)
        else:
            reply_text = _feedback_text(command_text, result, target)

        await message.channel.send(reply_text[:1800], reference=message, mention_author=False)

        print(
            f"[GCommand] executed guild={getattr(guild, 'id', None)} channel={getattr(message.channel, 'id', None)} command={content!r} targetVoice={target}",
            flush=True,
        )
    except Exception as exc:
        detail = str(getattr(exc, "detail", exc))[:1600]
        try:
            await message.channel.send(f"❌ {detail}", reference=message, mention_author=False)
        except Exception:
            pass
        print(f"[GCommand] failed command={content!r}: {type(exc).__name__}: {exc}", flush=True)
