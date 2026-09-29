import asyncio
import ipaddress
import math
import os
import socket
import struct
import tempfile
import wave
from typing import Any
from urllib.parse import urlparse

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
MAX_REPEAT = 10
MAX_MUSIC_SECONDS = 600

active_voice_client: Any | None = None
voice_lock = asyncio.Lock()
voice_volume = 0.85

SOUNDBOARD_NAMES = {
    "alerta": "Dois bipes de alerta",
    "erro": "Sequência descendente de erro",
    "sirene": "Sirene sintética curta",
    "airhorn": "Buzina sintética",
    "risada": "Risada robótica",
    "bruh": "BRUH robótico",
}


def voice_state_payload(vc: Any | None = None) -> dict[str, Any]:
    vc = vc or active_voice_client
    channel = getattr(vc, "channel", None) if vc else None
    guild = getattr(channel, "guild", None) if channel else None
    return {
        "connected": bool(vc and getattr(vc, "is_connected", lambda: False)()),
        "playing": bool(vc and getattr(vc, "is_playing", lambda: False)()),
        "channelId": str(channel.id) if channel else None,
        "channelName": getattr(channel, "name", None) if channel else None,
        "guildId": str(guild.id) if guild else None,
        "guildName": getattr(guild, "name", None) if guild else None,
        "volumePercent": round(voice_volume * 100),
    }


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


def stop_voice_playback() -> bool:
    vc = active_voice_client
    if vc is None:
        return False
    try:
        if vc.is_playing():
            vc.stop()
            return True
    except Exception:
        pass
    return False


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


def _write_wave(path: str, samples: list[float], sample_rate: int = 48000) -> None:
    with wave.open(path, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        frames = bytearray()
        for sample in samples:
            value = max(-1.0, min(1.0, sample))
            frames.extend(struct.pack("<h", int(value * 32767)))
        wav.writeframes(bytes(frames))


def _tone(freq: float, duration: float, sample_rate: int = 48000, gain: float = 0.55) -> list[float]:
    total = int(duration * sample_rate)
    fade = max(1, int(0.025 * sample_rate))
    out: list[float] = []
    for i in range(total):
        envelope = 1.0
        if i < fade:
            envelope = i / fade
        elif i > total - fade:
            envelope = max(0.0, (total - i) / fade)
        out.append(gain * envelope * math.sin(2 * math.pi * freq * i / sample_rate))
    return out


def synthesize_soundboard(name: str) -> str:
    name = name.casefold().strip()
    fd, path = tempfile.mkstemp(prefix="meudiscord-sfx-", suffix=".wav")
    os.close(fd)
    sr = 48000
    silence = [0.0] * int(0.10 * sr)

    if name == "alerta":
        samples = _tone(880, 0.22, sr) + silence + _tone(880, 0.22, sr)
    elif name == "erro":
        samples = _tone(620, 0.20, sr) + silence[:2400] + _tone(440, 0.22, sr) + silence[:2400] + _tone(260, 0.32, sr)
    elif name == "airhorn":
        duration = 1.35
        total = int(duration * sr)
        samples = []
        for i in range(total):
            t = i / sr
            env = min(1.0, t / 0.025) * max(0.0, 1.0 - (t / duration) * 0.22)
            sample = (
                0.43 * math.sin(2 * math.pi * 185 * t)
                + 0.30 * math.sin(2 * math.pi * 370 * t)
                + 0.18 * math.sin(2 * math.pi * 555 * t)
            )
            samples.append(env * sample)
    elif name == "sirene":
        duration = 3.2
        total = int(duration * sr)
        samples = []
        phase = 0.0
        for i in range(total):
            t = i / sr
            freq = 650 + 300 * (0.5 + 0.5 * math.sin(2 * math.pi * 0.75 * t))
            phase += 2 * math.pi * freq / sr
            samples.append(0.52 * math.sin(phase))
    else:
        try:
            os.unlink(path)
        except OSError:
            pass
        raise HTTPException(status_code=400, detail=f"Unknown sound: {name}")

    _write_wave(path, samples, sr)
    return path


async def play_audio_source(vc: Any, source: Any, timeout: int) -> None:
    if vc.is_playing():
        vc.stop()
        await asyncio.sleep(0.08)

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

    try:
        vc.play(source, after=after_playback)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Unable to start voice audio: {type(exc).__name__}: {exc}") from exc

    try:
        await asyncio.wait_for(finished, timeout=timeout)
    except asyncio.TimeoutError as exc:
        try:
            vc.stop()
        except Exception:
            pass
        raise HTTPException(status_code=504, detail="Voice playback timed out and was stopped") from exc
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Unable to play voice audio: {type(exc).__name__}: {exc}") from exc


def volume_source(source: Any) -> Any:
    try:
        return discord.PCMVolumeTransformer(source, volume=voice_volume)
    except Exception:
        return source


async def speak_robot(channel_id: str, text: str) -> dict[str, Any]:
    async with voice_lock:
        vc, channel = await ensure_voice_channel(channel_id)
        wav_path = await synthesize_robot_voice(text)
        source = volume_source(discord.FFmpegPCMAudio(
            wav_path,
            executable="ffmpeg",
            before_options="-nostdin",
            options="-vn",
        ))

    try:
        await play_audio_source(vc, source, timeout=180)
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
            "volumePercent": round(voice_volume * 100),
        },
    }


async def validate_public_https_url(url: str) -> str:
    parsed = urlparse(url.strip())
    if parsed.scheme.lower() != "https" or not parsed.hostname:
        raise HTTPException(status_code=400, detail="/play accepts only direct HTTPS audio URLs")
    host = parsed.hostname.casefold()
    if host in {"localhost", "localhost.localdomain"} or host.endswith(".local"):
        raise HTTPException(status_code=400, detail="Local/private audio URLs are not allowed")

    loop = asyncio.get_running_loop()
    try:
        records = await loop.run_in_executor(None, lambda: socket.getaddrinfo(host, parsed.port or 443, type=socket.SOCK_STREAM))
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Unable to resolve audio host: {exc}") from exc

    for record in records:
        ip = ipaddress.ip_address(record[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved or ip.is_unspecified:
            raise HTTPException(status_code=400, detail="Local/private audio URLs are not allowed")
    return url.strip()


async def play_remote_audio(channel_id: str, url: str) -> dict[str, Any]:
    url = await validate_public_https_url(url)
    async with voice_lock:
        vc, channel = await ensure_voice_channel(channel_id)
        source = volume_source(discord.FFmpegPCMAudio(
            url,
            executable="ffmpeg",
            before_options="-nostdin -reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
            options="-vn",
        ))
    await play_audio_source(vc, source, timeout=MAX_MUSIC_SECONDS)
    return {
        "ok": True,
        "mode": "audio-url",
        "url": url,
        "voice": voice_state_payload(vc),
    }


async def play_soundboard(channel_id: str, name: str) -> dict[str, Any]:
    name = name.casefold().strip()
    if name in {"risada", "bruh"}:
        text = "Ha ha ha ha ha ha ha!" if name == "risada" else "Bruh."
        result = await speak_robot(channel_id, text)
        result["sound"] = name
        return result

    async with voice_lock:
        vc, _channel = await ensure_voice_channel(channel_id)
        wav_path = synthesize_soundboard(name)
        source = volume_source(discord.FFmpegPCMAudio(wav_path, executable="ffmpeg", before_options="-nostdin", options="-vn"))
    try:
        await play_audio_source(vc, source, timeout=30)
    finally:
        try:
            os.unlink(wav_path)
        except OSError:
            pass
    return {"ok": True, "mode": "soundboard", "sound": name, "voice": voice_state_payload(vc)}


async def handle_voice_send(body: base.SendMessageBody) -> dict[str, Any]:
    global voice_volume
    target = body.to[len(VOICE_PREFIX):].strip()
    message = body.message.strip()
    folded = message.casefold()

    if target.casefold() in {"leave", "disconnect", "sair"}:
        return {"ok": True, "mode": "robot-voice", "disconnected": await disconnect_voice()}

    if not target:
        raise HTTPException(status_code=400, detail="Use to='voice:<channelId>' or to='voice:leave'")

    if folded in {"__join__", "/join", "entrar", "!join"}:
        async with voice_lock:
            vc, _channel = await ensure_voice_channel(target)
        return {"ok": True, "mode": "voice-command", "command": "join", "voice": voice_state_payload(vc)}

    if folded in {"/leave", "!leave", "/sair", "!sair"}:
        return {"ok": True, "mode": "voice-command", "command": "leave", "disconnected": await disconnect_voice()}

    if folded in {"/stop", "!stop", "/parar", "!parar"}:
        return {"ok": True, "mode": "voice-command", "command": "stop", "stopped": stop_voice_playback(), "voice": voice_state_payload()}

    if folded in {"/status", "!status"}:
        return {"ok": True, "mode": "voice-command", "command": "status", "voice": voice_state_payload()}

    if folded in {"/help", "!help", "/ajuda", "!ajuda"}:
        return {
            "ok": True,
            "mode": "voice-command",
            "commands": {
                "/join": "entra na call",
                "/leave": "sai da call",
                "/say <texto>": "fala com voz robótica",
                "/repeat <1-10> <texto>": "repete uma frase com limite",
                "/volume <0-200>": "ajusta o volume",
                "/stop": "para o áudio atual",
                "/play <https://...>": "toca uma URL HTTPS direta de áudio por até 10 minutos",
                "/sound <nome>": "toca um efeito do soundboard",
                "/sounds": "lista efeitos disponíveis",
                "/status": "mostra estado da call",
            },
        }

    if folded in {"/sounds", "!sounds", "/sons", "!sons"}:
        return {"ok": True, "mode": "soundboard", "sounds": SOUNDBOARD_NAMES}

    if folded.startswith(("/volume ", "!volume ")):
        raw = message.split(maxsplit=1)[1].strip()
        try:
            percent = int(raw)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Use /volume with a number from 0 to 200") from exc
        if percent < 0 or percent > 200:
            raise HTTPException(status_code=400, detail="Volume must be between 0 and 200")
        voice_volume = percent / 100.0
        return {"ok": True, "mode": "voice-command", "command": "volume", "volumePercent": percent}

    if folded.startswith(("/play ", "!play ")):
        url = message.split(maxsplit=1)[1].strip()
        return await play_remote_audio(target, url)

    if folded.startswith(("/sound ", "!sound ", "/som ", "!som ")):
        name = message.split(maxsplit=1)[1].strip()
        if name.casefold() not in SOUNDBOARD_NAMES:
            raise HTTPException(status_code=400, detail=f"Unknown sound. Available: {', '.join(SOUNDBOARD_NAMES)}")
        return await play_soundboard(target, name)

    if folded.startswith(("/repeat ", "!repeat ", "/repete ", "!repete ")):
        parts = message.split(maxsplit=2)
        if len(parts) < 3:
            raise HTTPException(status_code=400, detail="Use /repeat <1-10> <text>")
        try:
            count = int(parts[1])
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Repeat count must be a number") from exc
        if count < 1 or count > MAX_REPEAT:
            raise HTTPException(status_code=400, detail=f"Repeat count must be between 1 and {MAX_REPEAT}")
        repeated = " ... ".join([parts[2]] * count)
        return await speak_robot(target, repeated)

    if folded.startswith(("/say ", "!say ", "/fala ", "!fala ")):
        text = message.split(maxsplit=1)[1].strip()
        return await speak_robot(target, text)

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
