import asyncio
import base64
import io
import json
import mimetypes
import os
import pathlib
import re
import signal
import sqlite3
import time
from typing import Any

import aiohttp
from aiohttp import web
import discord

API_TOKEN = os.getenv("API_TOKEN", "")
DISCORD_USER_TOKEN = os.getenv("DISCORD_USER_TOKEN", "")
LISTEN_PORT = int(os.getenv("DISCORD_INTERNAL_PORT", "10004"))
MAX_MEDIA_BYTES = int(os.getenv("DISCORD_MAX_MEDIA_BYTES", str(24 * 1024 * 1024)))
SYNC_HISTORY = os.getenv("DISCORD_SYNC_HISTORY", "true").lower() in {"1", "true", "yes", "on"}
SYNC_MESSAGE_LIMIT = max(0, min(100, int(os.getenv("DISCORD_HISTORY_SYNC_LIMIT", "20"))))
SYNC_CHANNEL_LIMIT = max(1, min(500, int(os.getenv("DISCORD_HISTORY_SYNC_CHANNEL_LIMIT", "100"))))
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
GROQ_TRANSCRIBE_MODEL = os.getenv("GROQ_TRANSCRIBE_MODEL", "whisper-large-v3-turbo")
GROQ_TRANSCRIBE_LANGUAGE = os.getenv("GROQ_TRANSCRIBE_LANGUAGE", "pt")

AUDIO_EXTENSIONS = {".ogg", ".oga", ".mp3", ".wav", ".m4a", ".aac", ".flac", ".webm", ".mp4"}
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp"}


def now_ms() -> int:
    return int(time.time() * 1000)


def choose_db_path() -> pathlib.Path:
    configured = os.getenv("DISCORD_DB_PATH")
    if configured:
        return pathlib.Path(configured)
    data_dir = pathlib.Path("/data")
    if data_dir.exists() and os.access(data_dir, os.W_OK):
        return data_dir / "discord-self.sqlite"
    return pathlib.Path.cwd() / ".data" / "discord-self.sqlite"


def safe_json(value: Any) -> str | None:
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except Exception:
        return None


class DiscordStore:
    def __init__(self, db_path: pathlib.Path):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.path = str(db_path.resolve())
        self.db = sqlite3.connect(self.path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS discord_channels (
              channel_id TEXT PRIMARY KEY,
              guild_id TEXT,
              guild_name TEXT,
              name TEXT,
              type TEXT,
              last_ts INTEGER,
              updated_at INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS discord_messages (
              channel_id TEXT NOT NULL,
              message_id TEXT NOT NULL,
              guild_id TEXT,
              guild_name TEXT,
              channel_name TEXT,
              author_id TEXT,
              author_name TEXT,
              content TEXT,
              ts INTEGER,
              edited_ts INTEGER,
              attachments_json TEXT,
              referenced_message_id TEXT,
              raw_json TEXT,
              updated_at INTEGER NOT NULL,
              PRIMARY KEY (channel_id, message_id)
            );

            CREATE INDEX IF NOT EXISTS idx_discord_messages_channel_ts
              ON discord_messages(channel_id, ts DESC);
            CREATE INDEX IF NOT EXISTS idx_discord_messages_content
              ON discord_messages(content);
            """
        )
        self.db.commit()

    def upsert_channel(self, data: dict[str, Any]) -> None:
        channel_id = str(data.get("id") or data.get("channelId") or "").strip()
        if not channel_id:
            return
        ts = data.get("timestamp")
        self.db.execute(
            """
            INSERT INTO discord_channels(channel_id, guild_id, guild_name, name, type, last_ts, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(channel_id) DO UPDATE SET
              guild_id=COALESCE(excluded.guild_id, discord_channels.guild_id),
              guild_name=COALESCE(excluded.guild_name, discord_channels.guild_name),
              name=COALESCE(excluded.name, discord_channels.name),
              type=COALESCE(excluded.type, discord_channels.type),
              last_ts=CASE
                WHEN excluded.last_ts IS NULL THEN discord_channels.last_ts
                ELSE MAX(COALESCE(discord_channels.last_ts, 0), excluded.last_ts)
              END,
              updated_at=excluded.updated_at
            """,
            (
                channel_id,
                data.get("guildId"),
                data.get("guildName"),
                data.get("name"),
                data.get("type"),
                int(ts) if ts is not None else None,
                now_ms(),
            ),
        )
        self.db.commit()

    def upsert_message(self, data: dict[str, Any]) -> None:
        channel_id = str(data.get("channelId") or "").strip()
        message_id = str(data.get("id") or "").strip()
        if not channel_id or not message_id:
            return
        self.db.execute(
            """
            INSERT INTO discord_messages(
              channel_id, message_id, guild_id, guild_name, channel_name,
              author_id, author_name, content, ts, edited_ts,
              attachments_json, referenced_message_id, raw_json, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(channel_id, message_id) DO UPDATE SET
              guild_id=COALESCE(excluded.guild_id, discord_messages.guild_id),
              guild_name=COALESCE(excluded.guild_name, discord_messages.guild_name),
              channel_name=COALESCE(excluded.channel_name, discord_messages.channel_name),
              author_id=COALESCE(excluded.author_id, discord_messages.author_id),
              author_name=COALESCE(excluded.author_name, discord_messages.author_name),
              content=excluded.content,
              ts=COALESCE(excluded.ts, discord_messages.ts),
              edited_ts=COALESCE(excluded.edited_ts, discord_messages.edited_ts),
              attachments_json=COALESCE(excluded.attachments_json, discord_messages.attachments_json),
              referenced_message_id=COALESCE(excluded.referenced_message_id, discord_messages.referenced_message_id),
              raw_json=COALESCE(excluded.raw_json, discord_messages.raw_json),
              updated_at=excluded.updated_at
            """,
            (
                channel_id,
                message_id,
                data.get("guildId"),
                data.get("guildName"),
                data.get("channelName"),
                data.get("authorId"),
                data.get("authorName"),
                data.get("content") or "",
                data.get("timestamp"),
                data.get("editedTimestamp"),
                safe_json(data.get("attachments") or []),
                data.get("referencedMessageId"),
                safe_json(data),
                now_ms(),
            ),
        )
        self.upsert_channel(
            {
                "id": channel_id,
                "guildId": data.get("guildId"),
                "guildName": data.get("guildName"),
                "name": data.get("channelName"),
                "type": data.get("channelType"),
                "timestamp": data.get("timestamp"),
            }
        )
        self.db.commit()

    @staticmethod
    def row_to_message(row: sqlite3.Row) -> dict[str, Any]:
        try:
            attachments = json.loads(row["attachments_json"] or "[]")
        except Exception:
            attachments = []
        return {
            "id": row["message_id"],
            "channelId": row["channel_id"],
            "guildId": row["guild_id"],
            "guildName": row["guild_name"],
            "channelName": row["channel_name"],
            "authorId": row["author_id"],
            "authorName": row["author_name"],
            "content": row["content"] or "",
            "timestamp": row["ts"],
            "editedTimestamp": row["edited_ts"],
            "attachments": attachments,
            "referencedMessageId": row["referenced_message_id"],
        }

    def list_channels(self, limit: int = 100) -> list[dict[str, Any]]:
        rows = self.db.execute(
            """
            SELECT * FROM discord_channels
            ORDER BY COALESCE(last_ts, 0) DESC, updated_at DESC
            LIMIT ?
            """,
            (max(1, min(limit, 500)),),
        ).fetchall()
        return [
            {
                "id": row["channel_id"],
                "guildId": row["guild_id"],
                "guildName": row["guild_name"],
                "name": row["name"],
                "type": row["type"],
                "timestamp": row["last_ts"],
            }
            for row in rows
        ]

    def search(self, query: str, limit: int = 100) -> list[dict[str, Any]]:
        escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        rows = self.db.execute(
            """
            SELECT * FROM discord_messages
            WHERE content LIKE ? ESCAPE '\\'
            ORDER BY COALESCE(ts, 0) DESC
            LIMIT ?
            """,
            (f"%{escaped}%", max(1, min(limit, 500))),
        ).fetchall()
        return [{"channelId": row["channel_id"], "message": self.row_to_message(row)} for row in rows]

    def stats(self) -> dict[str, Any]:
        messages = self.db.execute("SELECT COUNT(*) FROM discord_messages").fetchone()[0]
        channels = self.db.execute("SELECT COUNT(*) FROM discord_channels").fetchone()[0]
        return {"path": self.path, "messages": int(messages), "channels": int(channels)}

    def close(self) -> None:
        try:
            self.db.close()
        except Exception:
            pass


store = DiscordStore(choose_db_path())
client = discord.Client()
http_session: aiohttp.ClientSession | None = None
ready_at: int | None = None
last_login_error: str | None = None
history_sync: dict[str, Any] = {
    "running": False,
    "channelsScanned": 0,
    "messagesStored": 0,
    "lastFinishedAt": None,
    "lastError": None,
}


def attachment_to_dict(attachment: discord.Attachment) -> dict[str, Any]:
    return {
        "id": str(attachment.id),
        "name": attachment.filename,
        "url": attachment.url,
        "proxyUrl": attachment.proxy_url,
        "contentType": getattr(attachment, "content_type", None),
        "size": attachment.size,
        "width": attachment.width,
        "height": attachment.height,
    }


def channel_name(channel: Any) -> str:
    if isinstance(channel, discord.DMChannel):
        recipient = getattr(channel, "recipient", None)
        return getattr(recipient, "global_name", None) or getattr(recipient, "name", None) or "DM"
    return getattr(channel, "name", None) or str(getattr(channel, "id", "unknown"))


def serialize_message(message: discord.Message) -> dict[str, Any]:
    guild = getattr(message, "guild", None)
    author = getattr(message, "author", None)
    channel = getattr(message, "channel", None)
    reference = getattr(message, "reference", None)
    created = getattr(message, "created_at", None)
    edited = getattr(message, "edited_at", None)
    return {
        "id": str(message.id),
        "channelId": str(message.channel.id),
        "channelName": channel_name(channel),
        "channelType": channel.__class__.__name__ if channel else None,
        "guildId": str(guild.id) if guild else None,
        "guildName": guild.name if guild else None,
        "authorId": str(author.id) if author else None,
        "authorName": (
            getattr(author, "display_name", None)
            or getattr(author, "global_name", None)
            or getattr(author, "name", None)
        ),
        "authorUsername": getattr(author, "name", None),
        "bot": bool(getattr(author, "bot", False)),
        "content": message.content or "",
        "timestamp": int(created.timestamp() * 1000) if created else None,
        "editedTimestamp": int(edited.timestamp() * 1000) if edited else None,
        "attachments": [attachment_to_dict(item) for item in message.attachments],
        "referencedMessageId": str(reference.message_id) if reference and reference.message_id else None,
    }


def remember_message(message: discord.Message) -> dict[str, Any]:
    data = serialize_message(message)
    store.upsert_message(data)
    return data


def remember_channel(channel: Any, timestamp: int | None = None) -> None:
    guild = getattr(channel, "guild", None)
    store.upsert_channel(
        {
            "id": str(channel.id),
            "guildId": str(guild.id) if guild else None,
            "guildName": guild.name if guild else None,
            "name": channel_name(channel),
            "type": channel.__class__.__name__,
            "timestamp": timestamp,
        }
    )


async def resolve_channel(channel_id: str) -> Any:
    value = str(channel_id or "").strip()
    if not value:
        raise ValueError("channelId é obrigatório.")
    try:
        numeric_id = int(value)
    except ValueError as exc:
        raise ValueError("channelId inválido.") from exc
    channel = client.get_channel(numeric_id)
    if channel is None:
        try:
            channel = await client.fetch_channel(numeric_id)
        except Exception as exc:
            raise ValueError("Canal do Discord não encontrado ou sem acesso para a conta.") from exc
    if not hasattr(channel, "send"):
        raise ValueError("O canal não suporta envio de mensagens.")
    remember_channel(channel)
    return channel


async def resolve_send_channel(channel_id: str | None, user_id: str | None) -> Any:
    if channel_id:
        return await resolve_channel(channel_id)
    if not user_id:
        raise ValueError("Informe channelId ou userId.")
    try:
        user = client.get_user(int(user_id)) or await client.fetch_user(int(user_id))
    except Exception as exc:
        raise ValueError("Usuário do Discord não encontrado.") from exc
    dm = getattr(user, "dm_channel", None)
    if dm is None:
        dm = await user.create_dm()
    remember_channel(dm)
    return dm


async def fetch_message(channel_id: str, message_id: str) -> tuple[Any, discord.Message]:
    channel = await resolve_channel(channel_id)
    try:
        message = await channel.fetch_message(int(message_id))
    except Exception as exc:
        raise ValueError("Mensagem do Discord não encontrada ou sem acesso.") from exc
    remember_message(message)
    return channel, message


async def download_media(url: str) -> tuple[bytes, str | None]:
    global http_session
    if http_session is None:
        http_session = aiohttp.ClientSession()
    async with http_session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as response:
        if response.status >= 400:
            raise ValueError(f"Falha ao baixar mídia: HTTP {response.status}")
        declared = int(response.headers.get("Content-Length", "0") or "0")
        if declared and declared > MAX_MEDIA_BYTES:
            raise ValueError(f"Mídia excede o limite de {MAX_MEDIA_BYTES} bytes.")
        data = await response.read()
        if len(data) > MAX_MEDIA_BYTES:
            raise ValueError(f"Mídia excede o limite de {MAX_MEDIA_BYTES} bytes.")
        return data, response.headers.get("Content-Type")


async def transcribe_groq(data: bytes, mimetype: str | None, filename: str) -> str | None:
    if not GROQ_API_KEY:
        return None
    global http_session
    if http_session is None:
        http_session = aiohttp.ClientSession()
    form = aiohttp.FormData()
    form.add_field("file", data, filename=filename, content_type=mimetype or "audio/ogg")
    form.add_field("model", GROQ_TRANSCRIBE_MODEL)
    if GROQ_TRANSCRIBE_LANGUAGE:
        form.add_field("language", GROQ_TRANSCRIBE_LANGUAGE)
    form.add_field("response_format", "json")
    async with http_session.post(
        "https://api.groq.com/openai/v1/audio/transcriptions",
        headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
        data=form,
        timeout=aiohttp.ClientTimeout(total=45),
    ) as response:
        text = await response.text()
        try:
            payload = json.loads(text) if text else {}
        except Exception:
            payload = {"raw": text}
        if response.status >= 400:
            error = payload.get("error")
            if isinstance(error, dict):
                error = error.get("message")
            raise ValueError(error or f"Groq retornou HTTP {response.status}")
        return payload.get("text")


async def sync_recent_history() -> None:
    if not SYNC_HISTORY or SYNC_MESSAGE_LIMIT <= 0 or history_sync["running"]:
        return
    history_sync.update({"running": True, "channelsScanned": 0, "messagesStored": 0, "lastError": None})
    try:
        channels: list[Any] = []
        for guild in client.guilds:
            for channel in getattr(guild, "channels", []):
                if len(channels) >= SYNC_CHANNEL_LIMIT:
                    break
                if not hasattr(channel, "history"):
                    continue
                remember_channel(channel)
                channels.append(channel)
            if len(channels) >= SYNC_CHANNEL_LIMIT:
                break
        for channel in list(getattr(client, "private_channels", [])):
            if len(channels) >= SYNC_CHANNEL_LIMIT:
                break
            if hasattr(channel, "history"):
                remember_channel(channel)
                channels.append(channel)

        for channel in channels:
            history_sync["channelsScanned"] += 1
            try:
                async for message in channel.history(limit=SYNC_MESSAGE_LIMIT, oldest_first=False):
                    remember_message(message)
                    history_sync["messagesStored"] += 1
            except Exception as exc:
                print(f"[DiscordSelf] Falha ao sincronizar {channel_name(channel)}: {exc}", flush=True)
        history_sync["lastFinishedAt"] = now_ms()
    except Exception as exc:
        history_sync["lastError"] = str(exc)
        print(f"[DiscordSelf] Erro na sincronização inicial: {exc}", flush=True)
    finally:
        history_sync["running"] = False


@client.event
async def on_ready() -> None:
    global ready_at, last_login_error
    ready_at = now_ms()
    last_login_error = None
    print(f"[DiscordSelf] Conectado como {client.user} ({client.user.id}) em {len(client.guilds)} servidor(es).", flush=True)
    for guild in client.guilds:
        for channel in getattr(guild, "channels", []):
            if hasattr(channel, "history"):
                remember_channel(channel)
    for channel in getattr(client, "private_channels", []):
        remember_channel(channel)
    asyncio.create_task(sync_recent_history())


@client.event
async def on_message(message: discord.Message) -> None:
    try:
        remember_message(message)
    except Exception as exc:
        print(f"[DiscordSelf] Falha ao persistir mensagem: {exc}", flush=True)


@client.event
async def on_message_edit(before: discord.Message, after: discord.Message) -> None:
    del before
    try:
        remember_message(after)
    except Exception as exc:
        print(f"[DiscordSelf] Falha ao atualizar mensagem persistida: {exc}", flush=True)


@web.middleware
async def auth_middleware(request: web.Request, handler):
    if not API_TOKEN:
        return web.json_response({"error": "API_TOKEN não configurado."}, status=503)
    if request.headers.get("Authorization", "") != f"Bearer {API_TOKEN}":
        return web.json_response({"error": "Não autorizado."}, status=401)
    return await handler(request)


def int_query(request: web.Request, name: str, default: int, min_value: int, max_value: int) -> int:
    try:
        value = int(request.query.get(name, str(default)))
    except ValueError:
        value = default
    return max(min_value, min(max_value, value))


async def route_status(_: web.Request) -> web.Response:
    return web.json_response(
        {
            "configured": bool(DISCORD_USER_TOKEN),
            "ready": client.is_ready(),
            "user": (
                {
                    "id": str(client.user.id),
                    "name": str(client.user),
                    "displayName": getattr(client.user, "display_name", None),
                }
                if client.user
                else None
            ),
            "guildCount": len(client.guilds),
            "privateChannelCount": len(getattr(client, "private_channels", [])),
            "readyAt": ready_at,
            "lastLoginError": last_login_error,
            "historySync": history_sync,
            "storage": store.stats(),
            "library": "discord.py-self",
            "libraryVersion": getattr(discord, "__version__", None),
        }
    )


async def route_guilds(_: web.Request) -> web.Response:
    guilds = [
        {
            "id": str(guild.id),
            "name": guild.name,
            "memberCount": getattr(guild, "member_count", None),
            "ownerId": str(guild.owner_id) if getattr(guild, "owner_id", None) else None,
        }
        for guild in client.guilds
    ]
    guilds.sort(key=lambda item: (item["name"] or "").lower())
    return web.json_response({"guilds": guilds})


async def route_chats(request: web.Request) -> web.Response:
    limit = int_query(request, "limit", 100, 1, 500)
    live: list[dict[str, Any]] = []
    for guild in client.guilds:
        for channel in getattr(guild, "channels", []):
            if not hasattr(channel, "history"):
                continue
            remember_channel(channel)
            live.append(
                {
                    "id": str(channel.id),
                    "name": channel_name(channel),
                    "guildId": str(guild.id),
                    "guildName": guild.name,
                    "type": channel.__class__.__name__,
                }
            )
    for channel in getattr(client, "private_channels", []):
        if not hasattr(channel, "history"):
            continue
        remember_channel(channel)
        live.append(
            {
                "id": str(channel.id),
                "name": channel_name(channel),
                "guildId": None,
                "guildName": None,
                "type": channel.__class__.__name__,
            }
        )
    deduped = list({item["id"]: item for item in live}.values())[:limit]
    return web.json_response({"channels": deduped, "persisted": store.list_channels(limit)})


async def route_messages(request: web.Request) -> web.Response:
    limit = int_query(request, "limit", 30, 1, 100)
    try:
        channel = await resolve_channel(request.match_info["channelId"])
        messages: list[dict[str, Any]] = []
        async for message in channel.history(limit=limit, oldest_first=True):
            messages.append(remember_message(message))
        return web.json_response({"channelId": str(channel.id), "messages": messages})
    except Exception as exc:
        return web.json_response({"error": str(exc)}, status=400)


async def route_search(request: web.Request) -> web.Response:
    query = request.query.get("q", "").strip()
    if not query:
        return web.json_response({"error": "q é obrigatório."}, status=400)
    limit = int_query(request, "limit", 100, 1, 500)
    return web.json_response({"query": query, "results": store.search(query, limit)})


async def route_stats(_: web.Request) -> web.Response:
    return web.json_response(store.stats())


async def route_send(request: web.Request) -> web.Response:
    try:
        body = await request.json()
        text = str(body.get("message") or "").strip()
        if not text:
            return web.json_response({"error": "message é obrigatório."}, status=400)
        channel = await resolve_send_channel(body.get("channelId"), body.get("userId"))
        user_ids = [str(value) for value in (body.get("mentionUserIds") or []) if str(value).strip()][:50]
        role_ids = [str(value) for value in (body.get("mentionRoleIds") or []) if str(value).strip()][:20]
        prefix = ""
        if body.get("prependMentions", True):
            prefix = " ".join([*[f"<@{value}>" for value in user_ids], *[f"<@&{value}>" for value in role_ids]])
        content = " ".join(value for value in [prefix, text] if value).strip()
        if len(content) > 2000:
            raise ValueError("Mensagem excede 2000 caracteres do Discord.")

        reference = None
        if body.get("replyToMessageId"):
            try:
                reference = await channel.fetch_message(int(body["replyToMessageId"]))
            except Exception:
                reference = None
        sent = await channel.send(
            content,
            reference=reference,
            mention_author=False,
            allowed_mentions=discord.AllowedMentions(
                users=True if user_ids else False,
                roles=True if role_ids else False,
                everyone=False,
                replied_user=False,
            ),
        )
        return web.json_response({"ok": True, "message": remember_message(sent)})
    except Exception as exc:
        return web.json_response({"error": str(exc)}, status=400)


async def route_react(request: web.Request) -> web.Response:
    try:
        body = await request.json()
        _, message = await fetch_message(body.get("channelId", ""), body.get("messageId", ""))
        emoji = str(body.get("emoji") or "").strip()
        if emoji:
            await message.add_reaction(emoji)
            return web.json_response({"ok": True, "action": "added", "emoji": emoji})
        removed = 0
        for reaction in list(message.reactions):
            try:
                await reaction.remove(client.user)
                removed += 1
            except Exception:
                pass
        return web.json_response({"ok": True, "action": "removed-own-reactions", "removed": removed})
    except Exception as exc:
        return web.json_response({"error": str(exc)}, status=400)


async def route_send_image(request: web.Request) -> web.Response:
    try:
        body = await request.json()
        channel = await resolve_send_channel(body.get("channelId"), body.get("userId"))
        image_url = body.get("imageUrl")
        image_base64 = body.get("imageBase64")
        mimetype = body.get("mimetype")
        if not image_url and not image_base64:
            return web.json_response({"error": "imageUrl ou imageBase64 é obrigatório."}, status=400)

        if image_url:
            data, detected_type = await download_media(str(image_url))
            mimetype = mimetype or detected_type
        else:
            raw = str(image_base64)
            raw = re.sub(r"^data:[^;]+;base64,", "", raw)
            data = base64.b64decode(raw)
            if not data:
                raise ValueError("imageBase64 inválido.")
            if len(data) > MAX_MEDIA_BYTES:
                raise ValueError(f"Imagem excede o limite de {MAX_MEDIA_BYTES} bytes.")

        extension = pathlib.Path(str(body.get("filename") or "")).suffix.lower()
        if not extension:
            extension = mimetypes.guess_extension((mimetype or "").split(";")[0]) or ".jpg"
        if extension not in IMAGE_EXTENSIONS:
            raise ValueError(f"Tipo de imagem não suportado: {mimetype or extension}")
        filename = re.sub(r"[^a-zA-Z0-9._-]", "_", str(body.get("filename") or f"discord-image{extension}"))

        user_ids = [str(value) for value in (body.get("mentionUserIds") or []) if str(value).strip()][:50]
        role_ids = [str(value) for value in (body.get("mentionRoleIds") or []) if str(value).strip()][:20]
        prefix = ""
        if body.get("prependMentions", True):
            prefix = " ".join([*[f"<@{value}>" for value in user_ids], *[f"<@&{value}>" for value in role_ids]])
        caption = " ".join(value for value in [prefix, str(body.get("caption") or "")] if value).strip()
        if len(caption) > 2000:
            raise ValueError("Legenda excede 2000 caracteres do Discord.")

        reference = None
        if body.get("replyToMessageId"):
            try:
                reference = await channel.fetch_message(int(body["replyToMessageId"]))
            except Exception:
                reference = None
        file = discord.File(io.BytesIO(data), filename=filename)
        sent = await channel.send(
            caption or None,
            file=file,
            reference=reference,
            mention_author=False,
            allowed_mentions=discord.AllowedMentions(
                users=True if user_ids else False,
                roles=True if role_ids else False,
                everyone=False,
                replied_user=False,
            ),
        )
        return web.json_response({"ok": True, "message": remember_message(sent)})
    except Exception as exc:
        return web.json_response({"error": str(exc)}, status=400)


async def route_audio(request: web.Request) -> web.Response:
    try:
        body = await request.json()
        _, message = await fetch_message(body.get("channelId", ""), body.get("messageId", ""))
        serialized = remember_message(message)
        audio = None
        for attachment in message.attachments:
            content_type = (getattr(attachment, "content_type", None) or "").lower()
            extension = pathlib.Path(attachment.filename or "").suffix.lower()
            if content_type.startswith("audio/") or extension in AUDIO_EXTENSIONS:
                audio = attachment
                break
        if audio is None:
            return web.json_response({"error": "Nenhum anexo de áudio encontrado nessa mensagem."}, status=404)

        data, detected_type = await download_media(audio.url)
        mimetype = getattr(audio, "content_type", None) or detected_type or "audio/ogg"
        filename = audio.filename or f"discord-audio{mimetypes.guess_extension(mimetype) or '.ogg'}"
        transcript = None
        transcription_error = None
        try:
            transcript = await transcribe_groq(data, mimetype, filename)
        except Exception as exc:
            transcription_error = str(exc)
        return web.json_response(
            {
                "channelId": str(message.channel.id),
                "messageId": str(message.id),
                "authorId": str(message.author.id) if message.author else None,
                "authorName": serialized.get("authorName"),
                "audioBase64": base64.b64encode(data).decode("ascii"),
                "mimetype": mimetype,
                "filename": filename,
                "size": len(data),
                "transcript": transcript,
                "transcriptionError": transcription_error,
            }
        )
    except Exception as exc:
        return web.json_response({"error": str(exc)}, status=400)


async def start_http() -> web.AppRunner:
    app = web.Application(middlewares=[auth_middleware], client_max_size=35 * 1024 * 1024)
    app.router.add_get("/discord/status", route_status)
    app.router.add_get("/discord/guilds", route_guilds)
    app.router.add_get("/discord/chats", route_chats)
    app.router.add_get("/discord/channels/{channelId}/messages", route_messages)
    app.router.add_get("/discord/search", route_search)
    app.router.add_get("/discord/db-stats", route_stats)
    app.router.add_post("/discord/send", route_send)
    app.router.add_post("/discord/react", route_react)
    app.router.add_post("/discord/send-image", route_send_image)
    app.router.add_post("/discord/audio", route_audio)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", LISTEN_PORT)
    await site.start()
    print(f"[DiscordSelf] Internal HTTP listening on 127.0.0.1:{LISTEN_PORT}", flush=True)
    return runner


async def run_discord() -> None:
    global last_login_error
    if not DISCORD_USER_TOKEN:
        last_login_error = "DISCORD_USER_TOKEN não configurado."
        print("[DiscordSelf] DISCORD_USER_TOKEN ausente; integração Discord ficará desativada.", flush=True)
        return
    try:
        await client.start(DISCORD_USER_TOKEN)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        last_login_error = str(exc)
        print(f"[DiscordSelf] Falha no login: {exc}", flush=True)


async def main() -> None:
    global http_session
    runner = await start_http()
    discord_task = asyncio.create_task(run_discord())
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:
            pass

    await stop_event.wait()
    discord_task.cancel()
    try:
        await discord_task
    except BaseException:
        pass
    try:
        await client.close()
    except Exception:
        pass
    if http_session is not None:
        await http_session.close()
    await runner.cleanup()
    store.close()


if __name__ == "__main__":
    asyncio.run(main())
