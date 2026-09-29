import asyncio
import subprocess

import discord
from fastapi import HTTPException

import bridge_music as music

app = music.app


async def streaming_music_worker() -> None:
    while True:
        async with music.music_condition:
            while not music.music_queue:
                await music.music_condition.wait()
            track = music.music_queue.pop(0)

        music.current_track = track
        music.current_audio_source = None
        music.current_ytdlp_process = None
        music.last_music_error = None

        try:
            vc, _channel = await music.voice.ensure_voice_channel(str(track["channelId"]))
            media_url = str(track.get("webpageUrl") or "").strip()
            if not media_url:
                raise HTTPException(status_code=400, detail="Track URL is missing")

            if track.get("direct"):
                media_url = await music.voice.validate_public_https_url(media_url)
                source = discord.FFmpegPCMAudio(
                    media_url,
                    executable="ffmpeg",
                    before_options="-nostdin -reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
                    options="-vn",
                )
            else:
                proc = subprocess.Popen(
                    [
                        "yt-dlp",
                        "--no-playlist",
                        "--no-warnings",
                        "--no-progress",
                        "--format", "bestaudio/best",
                        "--socket-timeout", "15",
                        "-o", "-",
                        media_url,
                    ],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                )
                music.current_ytdlp_process = proc
                if proc.stdout is None:
                    raise HTTPException(status_code=502, detail="yt-dlp audio pipe was not created")
                source = discord.FFmpegPCMAudio(
                    proc.stdout,
                    pipe=True,
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

            await music.voice.play_audio_source(vc, source, timeout=timeout)

            proc = getattr(music, "current_ytdlp_process", None)
            if proc is not None:
                return_code = proc.poll()
                if return_code not in (None, 0):
                    raise HTTPException(status_code=502, detail=f"yt-dlp stream exited with code {return_code}")

        except asyncio.CancelledError:
            raise
        except Exception as exc:
            music.last_music_error = f"{type(exc).__name__}: {exc}"
            print(f"[Music] {track.get('title')}: {music.last_music_error}", flush=True)
        finally:
            proc = getattr(music, "current_ytdlp_process", None)
            music.current_ytdlp_process = None
            if proc is not None and proc.poll() is None:
                try:
                    proc.terminate()
                    proc.wait(timeout=2)
                except Exception:
                    try:
                        proc.kill()
                    except Exception:
                        pass
            music.current_track = None
            music.current_audio_source = None


music.music_worker = streaming_music_worker
