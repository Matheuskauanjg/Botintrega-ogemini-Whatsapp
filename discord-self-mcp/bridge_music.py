import asyncio
import json
import os
from typing import Any
from urllib.parse import urlparse

import discord
from fastapi import Depends, HTTPException

import bridge_voice as voice

app = voice.app
base = voice.base
client = voice.client

MAX_TRACK_SECONDS = int(os.getenv("MUSIC_MAX_TRACK_SECONDS", "900"))
MAX_PLAYLIST_ITEMS = int(os.getenv("MUSIC_MAX_PLAYLIST_ITEMS", "20"))
MAX_QUEUE_ITEMS = int(os.getenv("MUSIC_MAX_QUEUE_ITEMS", "50"))
YTDLP_TIMEOUT_SECONDS = int(os.getenv("YTDLP_TIMEOUT_SECONDS", "45"))

music_volume = float(os.getenv("MUSIC_VOLUME", "0.75"))
music_volume = max(0.0, min(2.0, music_volume))

music_queue: list[dict[str, Any]] = []
music_condition = asyncio.Condition()
music_worker_task: asyncio.Task | None = None
current_track: dict[str, Any] | None = None
current_audio_source: Any | None = None
last_music_error: str | None = None


def _duration_text(seconds: Any) -> str | None:
    try:
        value = int(float(seconds))
    except (TypeError, ValueError):
        return None
    minutes, secs = divmod(value, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def public_track(track: dict[str, Any] | None) -> dict[str, Any] | None:
    if not track:
        return None
    return {
        "title": track.get("title"),
        "uploader": track.get("uploader"),
        "duration": track.get("duration"),
        "durationText": _duration_text(track.get("duration")),
        "webpageUrl": track.get("webpageUrl"),
        "extractor": track.get("extractor"),
        "requestedBy": track.get("requestedBy"),
        "channelId": track.get("channelId"),
        "direct": bool(track.get("direct")),
    }


def music_state_payload() -> dict[str, Any]:
    vc = voice.active_voice_client
    return {
        "nowPlaying": public_track(current_track),
        "paused": bool(vc and getattr(vc, "is_paused", lambda: False)()),
        "queueLength": len(music_queue),
        "queue": [public_track(item) for item in music_queue[:25]],
        "musicVolumePercent": round(music_volume * 100),
        "maxTrackSeconds": MAX_TRACK_SECONDS,
        "maxPlaylistItems": MAX_PLAYLIST_ITEMS,
        "lastError": last_music_error,
    }


async def run_ytdlp(*args: str, timeout: int | None = None) -> tuple[str, str]:
    proc = await asyncio.create_subprocess_exec(
        "yt-dlp",
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(),
            timeout=timeout or YTDLP_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError as exc:
        proc.kill()
        try:
            await proc.communicate()
        except Exception:
            pass
        raise HTTPException(status_code=504, detail="yt-dlp timed out") from exc

    out = stdout.decode("utf-8", errors="replace").strip()
    err = stderr.decode("utf-8", errors="replace").strip()
    if proc.returncode != 0:
        detail = err.splitlines()[-1] if err else f"yt-dlp exited with code {proc.returncode}"
        raise HTTPException(status_code=400, detail=f"Unable to resolve media: {detail}")
    return out, err


def _looks_like_url(value: str) -> bool:
    try:
        parsed = urlparse(value.strip())
        return parsed.scheme.lower() in {"http", "https"} and bool(parsed.hostname)
    except Exception:
        return False


def _youtube_watch_url(video_id: str) -> str:
    return f"https://www.youtube.com/watch?v={video_id}"


def normalize_media_entry(entry: dict[str, Any], fallback_url: str | None = None) -> dict[str, Any] | None:
    if not isinstance(entry, dict):
        return None

    duration = entry.get("duration")
    try:
        duration_value = int(float(duration)) if duration is not None else None
    except (TypeError, ValueError):
        duration_value = None

    if entry.get("is_live"):
        return None
    if duration_value and duration_value > MAX_TRACK_SECONDS:
        return None

    extractor = str(entry.get("extractor_key") or entry.get("extractor") or "").strip()
    video_id = str(entry.get("id") or "").strip()
    webpage = entry.get("webpage_url") or entry.get("original_url")
    raw_url = entry.get("url")

    if not webpage and "youtube" in extractor.casefold() and video_id:
        webpage = _youtube_watch_url(video_id)
    if not webpage and isinstance(raw_url, str) and raw_url.startswith("https://"):
        webpage = raw_url
    if not webpage and fallback_url:
        webpage = fallback_url

    if not webpage:
        return None

    title = str(entry.get("title") or entry.get("fulltitle") or video_id or webpage).strip()
    uploader = entry.get("uploader") or entry.get("channel") or entry.get("artist")

    return {
        "title": title[:500],
        "uploader": str(uploader)[:300] if uploader else None,
        "duration": duration_value,
        "webpageUrl": str(webpage),
        "extractor": extractor or None,
        "direct": False,
    }


async def resolve_media_input(query: str, *, search_count: int = 1) -> list[dict[str, Any]]:
    query = query.strip()
    if not query:
        raise HTTPException(status_code=400, detail="Media query cannot be empty")

    is_url = _looks_like_url(query)
    if is_url:
        if not query.lower().startswith("https://"):
            raise HTTPException(status_code=400, detail="Only HTTPS media links are accepted")
        await voice.validate_public_https_url(query)
        target = query
    else:
        count = max(1, min(5, int(search_count)))
        target = f"ytsearch{count}:{query}"

    args = [
        "--dump-single-json",
        "--flat-playlist",
        "--no-warnings",
        "--no-download",
        "--playlist-end", str(MAX_PLAYLIST_ITEMS),
        "--socket-timeout", "15",
        target,
    ]

    try:
        stdout, _stderr = await run_ytdlp(*args)
        payload = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=502, detail="yt-dlp returned invalid metadata") from exc
    except HTTPException:
        if is_url:
            return [{
                "title": query.rsplit("/", 1)[-1] or query,
                "uploader": urlparse(query).hostname,
                "duration": None,
                "webpageUrl": query,
                "extractor": "direct",
                "direct": True,
            }]
        raise

    entries = payload.get("entries") if isinstance(payload, dict) else None
    raw_entries: list[dict[str, Any]] = []

    if isinstance(entries, list):
        raw_entries = [item for item in entries if isinstance(item, dict)]
    elif isinstance(payload, dict):
        raw_entries = [payload]

    tracks: list[dict[str, Any]] = []
    for item in raw_entries[:MAX_PLAYLIST_ITEMS]:
        track = normalize_media_entry(item, fallback_url=query if is_url else None)
        if track:
            tracks.append(track)

    if not tracks and is_url:
        tracks = [{
            "title": query.rsplit("/", 1)[-1] or query,
            "uploader": urlparse(query).hostname,
            "duration": None,
            "webpageUrl": query,
            "extractor": "direct",
            "direct": True,
        }]

    if not tracks:
        raise HTTPException(
            status_code=400,
            detail=f"No playable media found. Live streams and tracks over {MAX_TRACK_SECONDS} seconds are skipped.",
        )

    return tracks


async def resolve_stream_url(track: dict[str, Any]) -> str:
    url = str(track.get("webpageUrl") or "").strip()
    if not url:
        raise HTTPException(status_code=400, detail="Track URL is missing")

    if track.get("direct"):
        return await voice.validate_public_https_url(url)

    stdout, _stderr = await run_ytdlp(
        "--no-playlist",
        "--no-warnings",
        "--format", "bestaudio/best",
        "--get-url",
        "--socket-timeout", "15",
        url,
    )
    stream_url = next((line.strip() for line in stdout.splitlines() if line.strip()), "")
    if not stream_url:
        raise HTTPException(status_code=502, detail="yt-dlp did not return an audio stream URL")
    if not stream_url.startswith("https://"):
        raise HTTPException(status_code=400, detail="Resolved media stream is not HTTPS")
    return await voice.validate_public_https_url(stream_url)


async def ensure_music_worker() -> None:
    global music_worker_task
    if music_worker_task is None or music_worker_task.done():
        music_worker_task = asyncio.create_task(music_worker(), name="meudiscord-music-worker")


async def enqueue_tracks(channel_id: str, tracks: list[dict[str, Any]], requested_by: str | None = None) -> list[dict[str, Any]]:
    await voice.ensure_voice_channel(channel_id)
    accepted: list[dict[str, Any]] = []

    async with music_condition:
        room = max(0, MAX_QUEUE_ITEMS - len(music_queue) - (1 if current_track else 0))
        for track in tracks[:room]:
            item = dict(track)
            item["channelId"] = channel_id
            item["requestedBy"] = requested_by
            music_queue.append(item)
            accepted.append(item)
        music_condition.notify_all()

    if not accepted:
        raise HTTPException(status_code=429, detail="Music queue is full")

    await ensure_music_worker()
    return accepted


async def music_worker() -> None:
    global current_track, current_audio_source, last_music_error

    while True:
        async with music_condition:
            while not music_queue:
                await music_condition.wait()
            track = music_queue.pop(0)

        current_track = track
        current_audio_source = None
        last_music_error = None

        try:
            vc, _channel = await voice.ensure_voice_channel(str(track["channelId"]))
            stream_url = await resolve_stream_url(track)

            source = discord.FFmpegPCMAudio(
                stream_url,
                executable="ffmpeg",
                before_options="-nostdin -reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
                options="-vn",
            )
            source = discord.PCMVolumeTransformer(source, volume=music_volume)
            current_audio_source = source

            duration = track.get("duration")
            if isinstance(duration, int) and duration > 0:
                timeout = min(MAX_TRACK_SECONDS + 30, max(60, duration + 30))
            else:
                timeout = MAX_TRACK_SECONDS + 30

            await voice.play_audio_source(vc, source, timeout=timeout)

        except asyncio.CancelledError:
            raise
        except Exception as exc:
            last_music_error = f"{type(exc).__name__}: {exc}"
            print(f"[Music] {track.get('title')}: {last_music_error}", flush=True)
        finally:
            current_track = None
            current_audio_source = None


async def clear_queue() -> int:
    async with music_condition:
        count = len(music_queue)
        music_queue.clear()
        return count


async def stop_music(*, clear: bool = True) -> dict[str, Any]:
    cleared = await clear_queue() if clear else 0
    vc = voice.active_voice_client
    stopped = False
    if vc is not None:
        try:
            if vc.is_playing() or vc.is_paused():
                vc.stop()
                stopped = True
        except Exception:
            pass
    return {"stopped": stopped, "cleared": cleared}


async def pause_music() -> bool:
    vc = voice.active_voice_client
    if vc is None:
        return False
    try:
        if vc.is_playing():
            vc.pause()
            return True
    except Exception:
        pass
    return False


async def resume_music() -> bool:
    vc = voice.active_voice_client
    if vc is None:
        return False
    try:
        if vc.is_paused():
            vc.resume()
            return True
    except Exception:
        pass
    return False


async def skip_music() -> bool:
    vc = voice.active_voice_client
    if vc is None:
        return False
    try:
        if vc.is_playing() or vc.is_paused():
            vc.stop()
            return True
    except Exception:
        pass
    return False


def voice_plus_music_state() -> dict[str, Any]:
    state = voice.voice_state_payload()
    state["music"] = music_state_payload()
    return state


async def handle_music_voice_send(body: base.SendMessageBody) -> dict[str, Any]:
    global music_volume

    target = body.to[len(voice.VOICE_PREFIX):].strip()
    message = body.message.strip()
    folded = message.casefold()

    if target.casefold() in {"leave", "disconnect", "sair"}:
        await stop_music(clear=True)
        return await voice.handle_voice_send(body)

    if folded in {"/leave", "!leave", "/sair", "!sair"}:
        await stop_music(clear=True)
        return await voice.handle_voice_send(body)

    if folded in {"/pause", "!pause"}:
        return {
            "ok": True,
            "mode": "music-command",
            "command": "pause",
            "paused": await pause_music(),
            "state": voice_plus_music_state(),
        }

    if folded in {"/resume", "!resume", "/continuar", "!continuar"}:
        return {
            "ok": True,
            "mode": "music-command",
            "command": "resume",
            "resumed": await resume_music(),
            "state": voice_plus_music_state(),
        }

    if folded in {"/skip", "!skip", "/pular", "!pular"}:
        return {
            "ok": True,
            "mode": "music-command",
            "command": "skip",
            "skipped": await skip_music(),
            "state": voice_plus_music_state(),
        }

    if folded in {"/stop", "!stop", "/parar", "!parar"}:
        result = await stop_music(clear=True)
        return {
            "ok": True,
            "mode": "music-command",
            "command": "stop",
            **result,
            "state": voice_plus_music_state(),
        }

    if folded in {"/clear", "!clear", "/limparfila", "!limparfila"}:
        cleared = await clear_queue()
        return {
            "ok": True,
            "mode": "music-command",
            "command": "clear",
            "cleared": cleared,
            "state": voice_plus_music_state(),
        }

    if folded in {"/queue", "!queue", "/fila", "!fila"}:
        return {
            "ok": True,
            "mode": "music-command",
            "command": "queue",
            "music": music_state_payload(),
        }

    if folded in {"/nowplaying", "!nowplaying", "/np", "!np", "/tocando", "!tocando"}:
        return {
            "ok": True,
            "mode": "music-command",
            "command": "nowplaying",
            "nowPlaying": public_track(current_track),
            "paused": bool(
                voice.active_voice_client
                and getattr(voice.active_voice_client, "is_paused", lambda: False)()
            ),
        }

    if folded in {"/status", "!status"}:
        return {
            "ok": True,
            "mode": "voice-command",
            "command": "status",
            "voice": voice_plus_music_state(),
        }

    if folded in {"/help", "!help", "/ajuda", "!ajuda"}:
        base_help = await voice.handle_voice_send(body)
        commands = dict(base_help.get("commands") or {})
        commands.update({
            "/play <link ou nome>": "toca YouTube, SoundCloud, playlist, URL direta ou pesquisa pelo nome",
            "/search <nome>": "pesquisa até 5 resultados sem tocar",
            "/pause": "pausa a música",
            "/resume": "continua a música",
            "/skip": "pula a faixa atual",
            "/queue": "mostra fila e faixa atual",
            "/nowplaying": "mostra o que está tocando",
            "/clear": "limpa a fila sem parar a faixa atual",
            "/stop": "para a faixa atual e limpa a fila",
            "/musicvolume <0-200>": "ajusta apenas o volume da música",
        })
        base_help["commands"] = commands
        base_help["musicLimits"] = {
            "maxTrackSeconds": MAX_TRACK_SECONDS,
            "maxPlaylistItems": MAX_PLAYLIST_ITEMS,
            "maxQueueItems": MAX_QUEUE_ITEMS,
        }
        return base_help

    if folded.startswith(("/musicvolume ", "!musicvolume ", "/volumemusica ", "!volumemusica ")):
        raw = message.split(maxsplit=1)[1].strip()
        try:
            percent = int(raw)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Use /musicvolume with a number from 0 to 200") from exc
        if percent < 0 or percent > 200:
            raise HTTPException(status_code=400, detail="Music volume must be between 0 and 200")
        music_volume = percent / 100.0
        if isinstance(current_audio_source, discord.PCMVolumeTransformer):
            current_audio_source.volume = music_volume
        return {
            "ok": True,
            "mode": "music-command",
            "command": "musicvolume",
            "musicVolumePercent": percent,
        }

    if folded.startswith(("/search ", "!search ", "/buscar ", "!buscar ")):
        query = message.split(maxsplit=1)[1].strip()
        tracks = await resolve_media_input(query, search_count=5)
        return {
            "ok": True,
            "mode": "music-search",
            "query": query,
            "results": [public_track(track) for track in tracks[:5]],
        }

    if folded.startswith(("/play ", "!play ", "/tocar ", "!tocar ")):
        query = message.split(maxsplit=1)[1].strip()
        tracks = await resolve_media_input(query, search_count=1)
        accepted = await enqueue_tracks(
            target,
            tracks,
            requested_by=str(getattr(client.user, "id", "")) or None,
        )
        return {
            "ok": True,
            "mode": "music-queue",
            "query": query,
            "added": len(accepted),
            "tracks": [public_track(item) for item in accepted[:10]],
            "queueLength": len(music_queue),
            "nowPlaying": public_track(current_track),
        }

    if current_track and (
        folded.startswith(("/say ", "!say ", "/fala ", "!fala ", "/sound ", "!sound ", "/som ", "!som ", "/repeat ", "!repeat ", "/repete ", "!repete "))
        or not folded.startswith("/")
    ):
        await stop_music(clear=True)

    return await voice.handle_voice_send(body)


for route in list(app.router.routes):
    if getattr(route, "path", None) == "/api/send" and "POST" in (getattr(route, "methods", set()) or set()):
        app.router.routes.remove(route)


@app.post("/api/send", dependencies=[Depends(base.require_api_token)])
async def send_message_with_music(body: base.SendMessageBody) -> dict[str, Any]:
    if body.to.casefold().startswith(voice.VOICE_PREFIX):
        return await handle_music_voice_send(body)
    return await voice.send_message_with_voice(body)
