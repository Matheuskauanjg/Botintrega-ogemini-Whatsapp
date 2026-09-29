import asyncio
import os
import tempfile
from typing import Any

import discord
from fastapi import Depends, HTTPException

import bridge as base

app = base.app
client = base.client

ROBOT_TTS_VOICE = os.getenv("ROBOT_TTS_VOICE", "pt-br").strip() or "pt-br"
ROBOT_TTS_SPEED = int(os.getenv("ROBOT_TTS_SPEED", "165"))
ROBOT_TTS_PITCH = int(os.getenv("ROBOT_TTS_PITCH", "35"))
ROBOT_TTS_MAX_CHARS = int(os.getenv("ROBOT_TTS_MAX_CHARS", "1200"))
VOICE_PREFIX = "voice:"

active_voice_client: Any | None = None
voice_lock = asyncio.Lock()


async def disconnect_voice() -> bool:
    global active_voice_client
    vc = active_voice_client
    active_voice_client = None
    if vc is None:
        return False
    try:
        if vc.is_playing():
            vc.stop()
    except Exception:
        pass
    try:
        await vc.disconnect(force=True)
    except TypeError:
        await vc.disconnect()
    except Exception:
        pass
    return True


async def resolve_voice_channel(channel_id: str):
    channel = await base.resolve_channel(channel_id)
    if not hasattr(channel, "connect"):
        raise HTTPException(status_code=400, detail="Channel cannot be joined as a voice call")
    return channel


async def ensure_voice_channel(channel_id: str):
    global active_voice_client
    channel = await resolve_voice_channel(channel_id)
    vc = active_voice_client

    if vc is not None and getattr(vc, "is_connected", lambda: False)():
        current = getattr(vc, "channel", None)
        if current is not None and int(current.id) == int(channel.id):
            return vc, channel

        current_guild = getattr(current, "guild", None)
        target_guild = getattr(channel, "guild", None)
        if current_guild is not None and target_guild is not None and current_guild.id == target_guild.id:
            try:
                await vc.move_to(channel)
                return vc, channel
            except Exception as exc:
                raise HTTPException(status_code=400, detail=f"Unable to move voice channel: {exc}") from exc

        await disconnect_voice()

    try:
        active_voice_client = await channel.connect(
            timeout=30,
            reconnect=True,
            self_deaf=False,
            self_mute=False,
        )
        return active_voice_client, channel
    except Exception as exc:
        raise HTTPException(
            status_code=400,
            detail=f"Unable to join voice channel: {type(exc).__name__}: {exc}",
        ) from exc


async def synthesize_robot_voice(text: str) -> str:
    text = text.strip()
    if not text:
        raise HTTPException(status_code=400, detail="Voice text cannot be empty")
    if len(text) > ROBOT_TTS_MAX_CHARS:
        raise HTTPException(
            status_code=400,
            detail=f"Voice text exceeds {ROBOT_TTS_MAX_CHARS} characters",
        )

    fd, path = tempfile.mkstemp(prefix="meudiscord-tts-", suffix=".wav")
    os.close(fd)

    proc = await asyncio.create_subprocess_exec(
        "espeak-ng",
        "-v", ROBOT_TTS_VOICE,
        "-s", str(ROBOT_TTS_SPEED),
        "-p", str(ROBOT_TTS_PITCH),
        "-w", path,
        text,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()

    if proc.returncode != 0:
        try:
            os.unlink(path)
        except OSError:
            pass
        detail = stderr.decode("utf-8", errors="replace").strip()
        raise HTTPException(status_code=500, detail=f"TTS failed: {detail or proc.returncode}")

    return path


async def speak_robot(channel_id: str, text: str) -> dict[str, Any]:
    async with voice_lock:
        vc, channel = await ensure_voice_channel(channel_id)
        wav_path = await synthesize_robot_voice(text)

        try:
            if vc.is_playing():
                vc.stop()

            source = discord.FFmpegPCMAudio(
                wav_path,
                executable="ffmpeg",
                before_options="-nostdin",
                options="-vn",
            )

            loop = asyncio.get_running_loop()
            finished = loop.create_future()

            def after_playback(error):
                def settle():
                    if finished.done():
                        return
                    if error:
                        finished.set_exception(error)
                    else:
                        finished.set_result(True)
                loop.call_soon_threadsafe(settle)

            vc.play(source, after=after_playback)

            try:
                await asyncio.wait_for(finished, timeout=180)
            except asyncio.TimeoutError as exc:
                try:
                    vc.stop()
                except Exception:
                    pass
                raise HTTPException(status_code=504, detail="Voice playback timed out") from exc
            except Exception as exc:
                raise HTTPException(
                    status_code=400,
                    detail=f"Unable to play voice audio: {type(exc).__name__}: {exc}",
                ) from exc
        finally:
            try:
                os.unlink(wav_path)
            except OSError:
                pass

    guild = getattr(channel, "guild", None)
    return {
        "ok": True,
        "mode": "robot-voice",
        "voice": {
            "channelId": str(channel.id),
            "channelName": getattr(channel, "name", str(channel.id)),
            "guildId": str(guild.id) if guild else None,
            "guildName": getattr(guild, "name", None) if guild else None,
            "connected": bool(getattr(vc, "is_connected", lambda: False)()),
            "spoken": True,
            "text": text,
            "tts": "espeak-ng",
            "voice": ROBOT_TTS_VOICE,
            "speed": ROBOT_TTS_SPEED,
            "pitch": ROBOT_TTS_PITCH,
        },
    }


async def handle_voice_send(body: base.SendMessageBody) -> dict[str, Any]:
    target = body.to[len(VOICE_PREFIX):].strip()

    if target.casefold() in {"leave", "disconnect", "sair"}:
        return {
            "ok": True,
            "mode": "robot-voice",
            "disconnected": await disconnect_voice(),
        }

    if not target:
        raise HTTPException(status_code=400, detail="Use to='voice:<channelId>' or to='voice:leave'")

    if body.message.strip().casefold() in {"__join__", "/join", "entrar"}:
        async with voice_lock:
            vc, channel = await ensure_voice_channel(target)
        guild = getattr(channel, "guild", None)
        return {
            "ok": True,
            "mode": "robot-voice",
            "voice": {
                "channelId": str(channel.id),
                "channelName": getattr(channel, "name", str(channel.id)),
                "guildId": str(guild.id) if guild else None,
                "guildName": getattr(guild, "name", None) if guild else None,
                "connected": bool(getattr(vc, "is_connected", lambda: False)()),
                "spoken": False,
            },
        }

    return await speak_robot(target, body.message)


# Replace the original POST /api/send handler while keeping the same MCP tool.
for route in list(app.router.routes):
    if getattr(route, "path", None) == "/api/send" and "POST" in (getattr(route, "methods", set()) or set()):
        app.router.routes.remove(route)


@app.post("/api/send", dependencies=[Depends(base.require_api_token)])
async def send_message_with_voice(body: base.SendMessageBody) -> dict[str, Any]:
    if body.to.casefold().startswith(VOICE_PREFIX):
        return await handle_voice_send(body)

    channel = await base.resolve_channel(body.to)
    reference = await base.fetch_message(channel, body.replyToMessageId) if body.replyToMessageId else None
    mention_ids = list(dict.fromkeys(body.mentionUserIds or []))

    if body.mentionAuthorOfMessageId:
        target = await base.fetch_message(channel, body.mentionAuthorOfMessageId)
        mention_ids.append(str(target.author.id))
        mention_ids = list(dict.fromkeys(mention_ids))

    content = body.message
    if body.prependMentions and mention_ids:
        content = " ".join(f"<@{user_id}>" for user_id in mention_ids) + " " + content

    try:
        sent = await channel.send(content, reference=reference, mention_author=False)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Unable to send message: {exc}") from exc

    return {"ok": True, "message": base.message_payload(sent)}


# Replace chat listing so voice/stage channels are discoverable through the existing MCP tool.
for route in list(app.router.routes):
    if getattr(route, "path", None) == "/api/chats" and "GET" in (getattr(route, "methods", set()) or set()):
        app.router.routes.remove(route)


@app.get("/api/chats", dependencies=[Depends(base.require_api_token)])
async def list_chats_with_voice(
    limit: int = base.Query(default=30, ge=1, le=100),
    includeGuilds: bool = True,
    includeDMs: bool = True,
) -> dict[str, Any]:
    await base.require_ready()
    channels: list[Any] = []

    if includeDMs:
        channels.extend(client.private_channels)

    if includeGuilds:
        for guild in client.guilds:
            channels.extend(guild.text_channels)
            channels.extend(getattr(guild, "voice_channels", []) or [])
            channels.extend(getattr(guild, "stage_channels", []) or [])

    channels = [channel for channel in channels if getattr(channel, "id", None)]
    channels.sort(
        key=lambda channel: base.snowflake_ts(getattr(channel, "last_message_id", None)),
        reverse=True,
    )

    return {
        "chats": [base.channel_payload(channel) for channel in channels[:limit]],
        "count": min(limit, len(channels)),
    }
