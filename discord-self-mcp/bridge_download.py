import asyncio
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any

import discord
from fastapi import HTTPException

import bridge_music as music

app = music.app

current_download_process: asyncio.subprocess.Process | None = None


async def _terminate_download() -> bool:
    global current_download_process
    proc = current_download_process
    current_download_process = None
    if proc is None or proc.returncode is not None:
        return False
    try:
        proc.terminate()
        await asyncio.wait_for(proc.wait(), timeout=2)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass
    return True


async def stop_music_with_download(*, clear: bool = True) -> dict[str, Any]:
    download_stopped = await _terminate_download()
    cleared = await music.clear_queue() if clear else 0
    vc = music.voice.active_voice_client
    stopped = False
    if vc is not None:
        try:
            if vc.is_playing() or vc.is_paused():
                vc.stop()
                stopped = True
        except Exception:
            pass
    return {"stopped": stopped, "downloadStopped": download_stopped, "cleared": cleared}


async def skip_music_with_download() -> bool:
    download_stopped = await _terminate_download()
    vc = music.voice.active_voice_client
    if vc is not None:
        try:
            if vc.is_playing() or vc.is_paused():
                vc.stop()
                return True
        except Exception:
            pass
    return download_stopped


async def download_track(track: dict[str, Any], directory: str) -> str:
    global current_download_process

    url = str(track.get("webpageUrl") or "").strip()
    if not url:
        raise HTTPException(status_code=400, detail="Track URL is missing")

    output_template = os.path.join(directory, "track.%(ext)s")
    args = [
        "yt-dlp",
        "--no-playlist",
        "--no-warnings",
        "--no-progress",
        "--format", "bestaudio/best",
        "--socket-timeout", "20",
        "--max-filesize", "120M",
        "-o", output_template,
        url,
    ]

    current_download_process = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    try:
        stdout, stderr = await asyncio.wait_for(current_download_process.communicate(), timeout=120)
    except asyncio.TimeoutError as exc:
        await _terminate_download()
        raise HTTPException(status_code=504, detail="Media download timed out") from exc
    finally:
        proc = current_download_process
        current_download_process = None

    if proc is None:
        raise HTTPException(status_code=499, detail="Media download was cancelled")

    if proc.returncode != 0:
        detail = stderr.decode("utf-8", errors="replace").strip()
        if detail:
            detail = detail.splitlines()[-1]
        raise HTTPException(status_code=400, detail=f"yt-dlp download failed: {detail or proc.returncode}")

    files = [p for p in Path(directory).glob("track.*") if p.is_file() and not p.name.endswith(".part")]
    if not files:
        out = stdout.decode("utf-8", errors="replace").strip()
        raise HTTPException(status_code=502, detail=f"yt-dlp produced no playable file: {out[-300:]}")

    files.sort(key=lambda p: p.stat().st_size, reverse=True)
    path = files[0]
    if path.stat().st_size <= 0:
        raise HTTPException(status_code=502, detail="Downloaded media file is empty")
    return str(path)


async def cached_music_worker() -> None:
    while True:
        async with music.music_condition:
            while not music.music_queue:
                await music.music_condition.wait()
            track = music.music_queue.pop(0)

        music.current_track = track
        music.current_audio_source = None
        music.last_music_error = None
        temp_dir = tempfile.mkdtemp(prefix="meudiscord-music-")

        try:
            vc, _channel = await music.voice.ensure_voice_channel(str(track["channelId"]))
            path = await download_track(track, temp_dir)

            # A voice connection can be replaced while the download runs; resolve it again.
            vc, _channel = await music.voice.ensure_voice_channel(str(track["channelId"]))

            source = discord.FFmpegPCMAudio(
                path,
                executable="ffmpeg",
                before_options="-nostdin",
                options="-vn",
            )
            source = discord.PCMVolumeTransformer(source, volume=music.music_volume)
            music.current_audio_source = source

            duration = track.get("duration")
            if isinstance(duration, int) and duration > 0:
                timeout = min(music.MAX_TRACK_SECONDS + 30, max(60, duration + 30))
            else:
                timeout = music.MAX_TRACK_SECONDS + 30

            print(f"[Music] Playing cached file: {track.get('title')} ({path})", flush=True)
            await music.voice.play_audio_source(vc, source, timeout=timeout)
            print(f"[Music] Playback finished: {track.get('title')}", flush=True)

        except asyncio.CancelledError:
            raise
        except Exception as exc:
            music.last_music_error = f"{type(exc).__name__}: {exc}"
            print(f"[Music] ERROR {track.get('title')}: {music.last_music_error}", flush=True)
        finally:
            await _terminate_download()
            music.current_track = None
            music.current_audio_source = None
            try:
                shutil.rmtree(temp_dir, ignore_errors=True)
            except Exception:
                pass


music.music_worker = cached_music_worker
music.stop_music = stop_music_with_download
music.skip_music = skip_music_with_download
