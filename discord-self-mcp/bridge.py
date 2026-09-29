import asyncio
import base64
import io
import os
import secrets
from contextlib import asynccontextmanager
from typing import Any

import discord
import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Query
from pydantic import BaseModel, Field

DISCORD_USER_TOKEN = os.getenv("DISCORD_USER_TOKEN", "").strip()
API_TOKEN = os.getenv("API_TOKEN", "").strip()
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "").strip()
GROQ_TRANSCRIBE_MODEL = os.getenv("GROQ_TRANSCRIBE_MODEL", "whisper-large-v3-turbo").strip()
GROQ_TRANSCRIBE_LANGUAGE = os.getenv("GROQ_TRANSCRIBE_LANGUAGE", "pt").strip()
MAX_MEDIA_BYTES = int(os.getenv("MAX_MEDIA_BYTES", str(10 * 1024 * 1024)))

client = discord.Client()
ready_event = asyncio.Event()
discord_task: asyncio.Task | None = None
last_client_error: str | None = None


def require_api_token(authorization: str | None = Header(default=None)) -> None:
    if not API_TOKEN:
        raise HTTPException(status_code=503, detail="API_TOKEN is not configured")
    expected = f"Bearer {API_TOKEN}"
    if not authorization or not secrets.compare_digest(authorization, expected):
        raise HTTPException(status_code=401, detail="Unauthorized")


@client.event
async def on_ready() -> None:
    global last_client_error
    last_client_error = None
    ready_event.set()
    print(f"[Discord] Logged in as {client.user} ({getattr(client.user, 'id', 'unknown')})", flush=True)


@client.event
async def on_disconnect() -> None:
    ready_event.clear()
    print("[Discord] Disconnected", flush=True)


async def run_discord() -> None:
    global last_client_error
    if not DISCORD_USER_TOKEN:
        print("[Discord] DISCORD_USER_TOKEN not configured; bridge will stay online in disconnected mode.", flush=True)
        return
    try:
        await client.start(DISCORD_USER_TOKEN)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        last_client_error = f"{type(exc).__name__}: {exc}"
        ready_event.clear()
        print(f"[Discord] Client failed: {last_client_error}", flush=True)


@asynccontextmanager
async def lifespan(_: FastAPI):
    global discord_task
    discord_task = asyncio.create_task(run_discord())
    try:
        yield
    finally:
        if not client.is_closed():
            try:
                await client.close()
            except Exception:
                pass
        if discord_task and not discord_task.done():
            discord_task.cancel()
            try:
                await discord_task
            except asyncio.CancelledError:
                pass


app = FastAPI(title="Meu Discord Bridge", version="1.0.0", lifespan=lifespan)


class SendMessageBody(BaseModel):
    to: str
    message: str = Field(min_length=1, max_length=5000)
    replyToMessageId: str | None = None
    mentionUserIds: list[str] | None = None
    mentionAuthorOfMessageId: str | None = None
    prependMentions: bool = True


class ReactBody(BaseModel):
    to: str
    messageId: str
    emoji: str = ""


class SendImageBody(BaseModel):
    to: str
    imageUrl: str | None = None
    imageBase64: str | None = None
    mimetype: str | None = None
    filename: str | None = None
    caption: str | None = Field(default=None, max_length=5000)
    replyToMessageId: str | None = None
    mentionUserIds: list[str] | None = None
    prependMentions: bool = True


class AudioBody(BaseModel):
    channelId: str
    messageId: str


def snowflake_ts(value: int | None) -> float:
    if not value:
        return 0.0
    try:
        return discord.utils.snowflake_time(int(value)).timestamp()
    except Exception:
        return 0.0


def channel_label(channel: Any) -> str:
    guild = getattr(channel, "guild", None)
    if guild is not None:
        return f"#{getattr(channel, 'name', channel.id)} — {guild.name}"
    recipients = getattr(channel, "recipients", None)
    if recipients:
        return ", ".join(getattr(user, "display_name", getattr(user, "name", str(user))) for user in recipients)
    recipient = getattr(channel, "recipient", None)
    if recipient is not None:
        return getattr(recipient, "display_name", getattr(recipient, "name", str(recipient)))
    return getattr(channel, "name", None) or str(getattr(channel, "id", "unknown"))


def channel_payload(channel: Any) -> dict[str, Any]:
    guild = getattr(channel, "guild", None)
    return {
        "id": str(channel.id),
        "name": channel_label(channel),
        "type": str(getattr(channel, "type", type(channel).__name__)),
        "guildId": str(guild.id) if guild else None,
        "guildName": guild.name if guild else None,
        "lastMessageId": str(getattr(channel, "last_message_id", "")) or None,
        "lastActivityAt": snowflake_ts(getattr(channel, "last_message_id", None)),
    }


def message_payload(message: discord.Message) -> dict[str, Any]:
    reference_id = None
    if message.reference is not None:
        reference_id = getattr(message.reference, "message_id", None)
    author = message.author
    return {
        "id": str(message.id),
        "channelId": str(message.channel.id),
        "authorId": str(author.id),
        "authorName": getattr(author, "display_name", None) or getattr(author, "name", str(author)),
        "authorUsername": getattr(author, "name", None),
        "content": message.content or "",
        "createdAt": message.created_at.isoformat(),
        "editedAt": message.edited_at.isoformat() if message.edited_at else None,
        "replyToMessageId": str(reference_id) if reference_id else None,
        "mentions": [
            {"id": str(user.id), "name": getattr(user, "display_name", None) or getattr(user, "name", str(user))}
            for user in getattr(message, "mentions", [])
        ],
        "attachments": [
            {
                "id": str(att.id),
                "filename": att.filename,
                "url": att.url,
                "size": att.size,
                "contentType": getattr(att, "content_type", None),
            }
            for att in message.attachments
        ],
    }


async def require_ready() -> None:
    if not DISCORD_USER_TOKEN:
        raise HTTPException(status_code=503, detail="DISCORD_USER_TOKEN is not configured")
    if not client.is_ready():
        detail = "Discord client is not connected"
        if last_client_error:
            detail += f": {last_client_error}"
        raise HTTPException(status_code=503, detail=detail)


async def resolve_channel(channel_id: str):
    await require_ready()
    try:
        cid = int(channel_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="channel id must be numeric") from exc
    channel = client.get_channel(cid)
    if channel is None:
        try:
            channel = await client.fetch_channel(cid)
        except Exception as exc:
            raise HTTPException(status_code=404, detail=f"Channel not found: {exc}") from exc
    return channel


async def fetch_message(channel, message_id: str) -> discord.Message:
    try:
        mid = int(message_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="message id must be numeric") from exc
    try:
        return await channel.fetch_message(mid)
    except Exception as exc:
        raise HTTPException(status_code=404, detail=f"Message not found: {exc}") from exc


async def read_remote_bytes(url: str) -> bytes:
    if not url.lower().startswith("https://"):
        raise HTTPException(status_code=400, detail="Only HTTPS image URLs are accepted")
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as http:
        response = await http.get(url)
        response.raise_for_status()
        data = response.content
    if len(data) > MAX_MEDIA_BYTES:
        raise HTTPException(status_code=413, detail="Media exceeds size limit")
    return data


async def transcribe_audio(data: bytes, filename: str, mimetype: str) -> str | None:
    if not GROQ_API_KEY:
        return None
    headers = {"Authorization": f"Bearer {GROQ_API_KEY}"}
    form = {"model": GROQ_TRANSCRIBE_MODEL, "response_format": "json"}
    if GROQ_TRANSCRIBE_LANGUAGE:
        form["language"] = GROQ_TRANSCRIBE_LANGUAGE
    files = {"file": (filename, data, mimetype)}
    async with httpx.AsyncClient(timeout=90) as http:
        response = await http.post("https://api.groq.com/openai/v1/audio/transcriptions", headers=headers, data=form, files=files)
        if response.is_error:
            return None
        payload = response.json()
    return payload.get("text")


@app.get("/health")
async def health() -> dict[str, Any]:
    return {"ok": True, "discordReady": client.is_ready(), "tokenConfigured": bool(DISCORD_USER_TOKEN)}


@app.get("/api/status", dependencies=[Depends(require_api_token)])
async def status() -> dict[str, Any]:
    user = client.user if client.is_ready() else None
    return {
        "tokenConfigured": bool(DISCORD_USER_TOKEN),
        "connected": client.is_ready(),
        "user": {
            "id": str(user.id),
            "name": getattr(user, "name", None),
            "displayName": getattr(user, "display_name", None),
        } if user else None,
        "guildCount": len(client.guilds) if client.is_ready() else 0,
        "privateChannelCount": len(client.private_channels) if client.is_ready() else 0,
        "latencyMs": round(client.latency * 1000) if client.is_ready() else None,
        "lastError": last_client_error,
        "library": "discord.py-self",
    }


@app.get("/api/chats", dependencies=[Depends(require_api_token)])
async def list_chats(
    limit: int = Query(default=30, ge=1, le=100),
    includeGuilds: bool = True,
    includeDMs: bool = True,
) -> dict[str, Any]:
    await require_ready()
    channels: list[Any] = []
    if includeDMs:
        channels.extend(client.private_channels)
    if includeGuilds:
        for guild in client.guilds:
            channels.extend(guild.text_channels)
    channels = [channel for channel in channels if getattr(channel, "id", None)]
    channels.sort(key=lambda channel: snowflake_ts(getattr(channel, "last_message_id", None)), reverse=True)
    return {"chats": [channel_payload(channel) for channel in channels[:limit]], "count": min(limit, len(channels))}


@app.get("/api/chats/{channel_id}/messages", dependencies=[Depends(require_api_token)])
async def read_messages(channel_id: str, limit: int = Query(default=30, ge=1, le=100)) -> dict[str, Any]:
    channel = await resolve_channel(channel_id)
    try:
        messages = [message async for message in channel.history(limit=limit)]
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Unable to read channel history: {exc}") from exc
    messages.reverse()
    return {"channel": channel_payload(channel), "messages": [message_payload(message) for message in messages]}


@app.get("/api/search", dependencies=[Depends(require_api_token)])
async def search_messages(
    q: str = Query(min_length=1, max_length=500),
    limit: int = Query(default=30, ge=1, le=100),
    maxChannels: int = Query(default=15, ge=1, le=40),
    perChannel: int = Query(default=50, ge=1, le=100),
) -> dict[str, Any]:
    await require_ready()
    needle = q.casefold()
    channels: list[Any] = list(client.private_channels)
    for guild in client.guilds:
        channels.extend(guild.text_channels)
    channels.sort(key=lambda channel: snowflake_ts(getattr(channel, "last_message_id", None)), reverse=True)
    results: list[dict[str, Any]] = []
    scanned = 0
    for channel in channels[:maxChannels]:
        try:
            async for message in channel.history(limit=perChannel):
                scanned += 1
                if needle in (message.content or "").casefold():
                    item = message_payload(message)
                    item["channelName"] = channel_label(channel)
                    results.append(item)
                    if len(results) >= limit:
                        return {"query": q, "results": results, "scannedMessages": scanned, "truncated": True}
        except Exception:
            continue
    return {"query": q, "results": results, "scannedMessages": scanned, "truncated": False}


@app.get("/api/cache-stats", dependencies=[Depends(require_api_token)])
async def cache_stats() -> dict[str, Any]:
    await require_ready()
    cached_messages = len(getattr(client, "cached_messages", []) or [])
    return {
        "guilds": len(client.guilds),
        "privateChannels": len(client.private_channels),
        "cachedMessages": cached_messages,
        "users": len(getattr(client, "users", []) or []),
    }


@app.post("/api/send", dependencies=[Depends(require_api_token)])
async def send_message(body: SendMessageBody) -> dict[str, Any]:
    channel = await resolve_channel(body.to)
    reference = await fetch_message(channel, body.replyToMessageId) if body.replyToMessageId else None
    mention_ids = list(dict.fromkeys(body.mentionUserIds or []))
    if body.mentionAuthorOfMessageId:
        target = await fetch_message(channel, body.mentionAuthorOfMessageId)
        mention_ids.append(str(target.author.id))
        mention_ids = list(dict.fromkeys(mention_ids))
    content = body.message
    if body.prependMentions and mention_ids:
        content = " ".join(f"<@{user_id}>" for user_id in mention_ids) + " " + content
    try:
        sent = await channel.send(content, reference=reference, mention_author=False)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Unable to send message: {exc}") from exc
    return {"ok": True, "message": message_payload(sent)}


@app.post("/api/react", dependencies=[Depends(require_api_token)])
async def react_message(body: ReactBody) -> dict[str, Any]:
    channel = await resolve_channel(body.to)
    message = await fetch_message(channel, body.messageId)
    try:
        if body.emoji:
            await message.add_reaction(body.emoji)
        else:
            for reaction in list(message.reactions):
                try:
                    await reaction.remove(client.user)
                except Exception:
                    continue
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Unable to update reaction: {exc}") from exc
    return {"ok": True, "channelId": body.to, "messageId": body.messageId, "emoji": body.emoji}


@app.post("/api/send-image", dependencies=[Depends(require_api_token)])
async def send_image(body: SendImageBody) -> dict[str, Any]:
    if not body.imageUrl and not body.imageBase64:
        raise HTTPException(status_code=400, detail="imageUrl or imageBase64 is required")
    channel = await resolve_channel(body.to)
    reference = await fetch_message(channel, body.replyToMessageId) if body.replyToMessageId else None
    if body.imageBase64:
        try:
            raw = body.imageBase64.split(",", 1)[-1]
            data = base64.b64decode(raw, validate=True)
        except Exception as exc:
            raise HTTPException(status_code=400, detail="Invalid imageBase64") from exc
    else:
        data = await read_remote_bytes(body.imageUrl or "")
    if len(data) > MAX_MEDIA_BYTES:
        raise HTTPException(status_code=413, detail="Media exceeds size limit")
    filename = body.filename or "image.jpg"
    content = body.caption or ""
    mention_ids = list(dict.fromkeys(body.mentionUserIds or []))
    if body.prependMentions and mention_ids:
        content = " ".join(f"<@{user_id}>" for user_id in mention_ids) + (" " + content if content else "")
    file = discord.File(io.BytesIO(data), filename=filename)
    try:
        sent = await channel.send(content=content or None, file=file, reference=reference, mention_author=False)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Unable to send image: {exc}") from exc
    return {"ok": True, "message": message_payload(sent)}


@app.post("/api/audio", dependencies=[Depends(require_api_token)])
async def read_audio(body: AudioBody) -> dict[str, Any]:
    channel = await resolve_channel(body.channelId)
    message = await fetch_message(channel, body.messageId)
    attachment = None
    for item in message.attachments:
        content_type = (getattr(item, "content_type", None) or "").lower()
        filename = (item.filename or "").lower()
        if content_type.startswith("audio/") or filename.endswith((".ogg", ".opus", ".mp3", ".m4a", ".wav", ".webm")):
            attachment = item
            break
    if attachment is None:
        raise HTTPException(status_code=404, detail="No audio attachment found in this message")
    if attachment.size and attachment.size > MAX_MEDIA_BYTES:
        raise HTTPException(status_code=413, detail="Audio exceeds size limit")
    try:
        data = await attachment.read()
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Unable to download audio: {exc}") from exc
    mimetype = getattr(attachment, "content_type", None) or "audio/ogg"
    transcript = await transcribe_audio(data, attachment.filename or "audio.ogg", mimetype)
    return {
        "channelId": body.channelId,
        "messageId": body.messageId,
        "filename": attachment.filename,
        "mimetype": mimetype,
        "audioBase64": base64.b64encode(data).decode("ascii"),
        "transcript": transcript,
        "transcriptionProvider": "groq" if transcript else None,
    }
