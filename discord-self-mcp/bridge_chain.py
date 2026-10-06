import asyncio
import os
import re
import sqlite3
import time
import unicodedata
from pathlib import Path
from typing import Any

import httpx
from fastapi import Depends

import bridge_gprefix as stack

app = stack.app
fun = stack.fun
auto = fun.auto
base = auto.base
client = auto.client

# Save the original Groq handlers before bridge_gemini replaces them.
_original_auto_generate = auto._generate_reply
_original_ask_groq = fun._ask_groq
_original_recent_context = auto._recent_context

import bridge_gemini as gemini  # noqa: E402

# ---------------------------------------------------------------------------
# Persistent conversation context
# ---------------------------------------------------------------------------
CONTEXT_DB_PATH = os.getenv("CONTEXT_DB_PATH", "/data/discord_context.sqlite3").strip()
CONTEXT_RETENTION_DAYS = max(1, int(os.getenv("CONTEXT_RETENTION_DAYS", "90")))
CONTEXT_MAX_ROWS_PER_CHANNEL = max(500, int(os.getenv("CONTEXT_MAX_ROWS_PER_CHANNEL", "20000")))
CONTEXT_PROMPT_MESSAGES = max(10, min(200, int(os.getenv("CONTEXT_PROMPT_MESSAGES", "80"))))
CONTEXT_PROMPT_CHARS = max(2000, min(50000, int(os.getenv("CONTEXT_PROMPT_CHARS", "18000"))))
CONTEXT_LONGTERM_ITEMS = max(8, min(60, int(os.getenv("CONTEXT_LONGTERM_ITEMS", "24"))))
CONTEXT_LONGTERM_CHARS = max(2000, min(24000, int(os.getenv("CONTEXT_LONGTERM_CHARS", "9000"))))
CONTEXT_BACKFILL_MESSAGES = max(
    CONTEXT_PROMPT_MESSAGES,
    min(CONTEXT_MAX_ROWS_PER_CHANNEL, int(os.getenv("CONTEXT_BACKFILL_MESSAGES", "3000"))),
)
_backfilled_channels: set[str] = set()

# Durable memory mirror (Supabase). SQLite stays as a fast local cache.
SUPABASE_URL = os.getenv("SUPABASE_URL", "").strip().rstrip("/")
SUPABASE_ANON_KEY = os.getenv("SUPABASE_ANON_KEY", "").strip()
SUPABASE_BOT_SECRET = os.getenv("SUPABASE_BOT_SECRET", "").strip()
SUPABASE_SYNC_ENABLED = bool(SUPABASE_URL and SUPABASE_ANON_KEY and SUPABASE_BOT_SECRET)
SUPABASE_SYNC_TIMEOUT = max(2.0, min(20.0, float(os.getenv("SUPABASE_SYNC_TIMEOUT", "6"))))
_supabase_stats: dict[str, Any] = {
    "enabled": SUPABASE_SYNC_ENABLED,
    "messageWrites": 0,
    "socialWrites": 0,
    "reads": 0,
    "failures": 0,
    "lastError": None,
    "lastSyncAt": None,
}

_default_context_channels = [
    os.getenv("AUTO_REPLY_CHANNEL_ID", "1554920683786739712").strip(),
    os.getenv("G_COMMAND_CHANNEL_ID", "1554920170659774545").strip(),
]
CONTEXT_CHANNEL_IDS = {
    item.strip()
    for item in os.getenv("CONTEXT_CHANNEL_IDS", ",".join(_default_context_channels)).split(",")
    if item.strip()
}


def _supabase_headers(prefer: str | None = None) -> dict[str, str]:
    headers = {
        "apikey": SUPABASE_ANON_KEY,
        "Authorization": f"Bearer {SUPABASE_ANON_KEY}",
        "x-bot-secret": SUPABASE_BOT_SECRET,
        "Content-Type": "application/json",
    }
    if prefer:
        headers["Prefer"] = prefer
    return headers


def _supabase_request_sync(
    method: str,
    table: str,
    *,
    params: dict[str, str] | None = None,
    payload: Any = None,
    prefer: str | None = None,
) -> Any:
    if not SUPABASE_SYNC_ENABLED:
        return None
    try:
        with httpx.Client(timeout=SUPABASE_SYNC_TIMEOUT) as http:
            response = http.request(
                method,
                f"{SUPABASE_URL}/rest/v1/{table}",
                headers=_supabase_headers(prefer),
                params=params,
                json=payload,
            )
        if response.is_error:
            raise RuntimeError(f"Supabase HTTP {response.status_code}: {response.text[:240]}")
        _supabase_stats["lastError"] = None
        _supabase_stats["lastSyncAt"] = time.time()
        if not response.content:
            return None
        return response.json()
    except Exception as exc:
        _supabase_stats["failures"] = int(_supabase_stats.get("failures", 0)) + 1
        _supabase_stats["lastError"] = f"{type(exc).__name__}: {exc}"[:500]
        print(f"[SupabaseMemory] {method} {table} failed: {type(exc).__name__}: {exc}", flush=True)
        return None


def _supabase_store_messages_sync(rows: list[dict[str, Any]]) -> None:
    if not SUPABASE_SYNC_ENABLED or not rows:
        return
    payload = [
        {
            "message_id": str(row.get("message_id") or ""),
            "guild_id": str(row.get("guild_id") or ""),
            "channel_id": str(row.get("channel_id") or ""),
            "author_id": str(row.get("author_id") or ""),
            "author_name": str(row.get("author_name") or "alguém"),
            "content": str(row.get("content") or "")[:4000],
            "reply_to_message_id": row.get("reply_to_message_id"),
            "is_self": bool(row.get("is_self")),
            "created_at": float(row.get("created_at") or time.time()),
        }
        for row in rows
        if row.get("message_id") and row.get("channel_id")
    ]
    for offset in range(0, len(payload), 200):
        chunk = payload[offset:offset + 200]
        result = _supabase_request_sync(
            "POST",
            "discord_messages",
            params={"on_conflict": "message_id"},
            payload=chunk,
            prefer="resolution=merge-duplicates,return=minimal",
        )
        if result is not None or _supabase_stats.get("lastError") is None:
            _supabase_stats["messageWrites"] = int(_supabase_stats.get("messageWrites", 0)) + len(chunk)


def _supabase_get_social_state_sync(guild_id: str, user_id: str) -> dict[str, Any] | None:
    result = _supabase_request_sync(
        "GET",
        "discord_social_states",
        params={
            "select": "guild_id,user_id,user_name,friendship,trust,respect,stress,sadness,irritation,energy,interactions,updated_at",
            "guild_id": f"eq.{guild_id}",
            "user_id": f"eq.{user_id}",
            "limit": "1",
        },
    )
    if isinstance(result, list) and result:
        _supabase_stats["reads"] = int(_supabase_stats.get("reads", 0)) + 1
        return result[0]
    return None


def _supabase_upsert_social_state_sync(guild_id: str, user_id: str, state: dict[str, Any]) -> None:
    if not SUPABASE_SYNC_ENABLED:
        return
    payload = {
        "guild_id": guild_id,
        "user_id": user_id,
        "user_name": str(state.get("userName") or "alguém"),
        "friendship": int(state.get("friendship", 50)),
        "trust": int(state.get("trust", 50)),
        "respect": int(state.get("respect", 50)),
        "stress": int(state.get("stress", 10)),
        "sadness": int(state.get("sadness", 5)),
        "irritation": int(state.get("irritation", 5)),
        "energy": int(state.get("energy", 70)),
        "interactions": int(state.get("interactions", 0)),
        "updated_at": time.time(),
    }
    _supabase_request_sync(
        "POST",
        "discord_social_states",
        params={"on_conflict": "guild_id,user_id"},
        payload=payload,
        prefer="resolution=merge-duplicates,return=minimal",
    )
    if _supabase_stats.get("lastError") is None:
        _supabase_stats["socialWrites"] = int(_supabase_stats.get("socialWrites", 0)) + 1


def _open_context_db() -> sqlite3.Connection:
    path = Path(CONTEXT_DB_PATH)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        path = Path("/tmp/discord_context.sqlite3")
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS messages (
            message_id TEXT PRIMARY KEY,
            guild_id TEXT NOT NULL,
            channel_id TEXT NOT NULL,
            author_id TEXT NOT NULL,
            author_name TEXT NOT NULL,
            content TEXT NOT NULL,
            reply_to_message_id TEXT,
            is_self INTEGER NOT NULL DEFAULT 0,
            created_at REAL NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_messages_channel_time ON messages(channel_id, created_at DESC)"
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS social_states (
            guild_id TEXT NOT NULL,
            user_id TEXT NOT NULL,
            user_name TEXT NOT NULL,
            friendship INTEGER NOT NULL DEFAULT 50,
            trust INTEGER NOT NULL DEFAULT 50,
            respect INTEGER NOT NULL DEFAULT 50,
            stress INTEGER NOT NULL DEFAULT 10,
            sadness INTEGER NOT NULL DEFAULT 5,
            irritation INTEGER NOT NULL DEFAULT 5,
            energy INTEGER NOT NULL DEFAULT 70,
            interactions INTEGER NOT NULL DEFAULT 0,
            updated_at REAL NOT NULL,
            PRIMARY KEY (guild_id, user_id)
        )
        """
    )
    conn.commit()
    return conn


def _store_row_sync(row: dict[str, Any]) -> None:
    if not row.get("message_id") or not row.get("channel_id"):
        return
    with _open_context_db() as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO messages (
                message_id, guild_id, channel_id, author_id, author_name,
                content, reply_to_message_id, is_self, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row["message_id"],
                row.get("guild_id", ""),
                row["channel_id"],
                row.get("author_id", ""),
                row.get("author_name", "alguém"),
                row.get("content", "")[:4000],
                row.get("reply_to_message_id"),
                1 if row.get("is_self") else 0,
                float(row.get("created_at") or time.time()),
            ),
        )
        cutoff = time.time() - CONTEXT_RETENTION_DAYS * 86400
        conn.execute("DELETE FROM messages WHERE created_at < ?", (cutoff,))
        conn.execute(
            """
            DELETE FROM messages
            WHERE channel_id = ? AND message_id NOT IN (
                SELECT message_id FROM messages
                WHERE channel_id = ?
                ORDER BY created_at DESC
                LIMIT ?
            )
            """,
            (row["channel_id"], row["channel_id"], CONTEXT_MAX_ROWS_PER_CHANNEL),
        )
        conn.commit()
    _supabase_store_messages_sync([row])


def _store_rows_sync(rows: list[dict[str, Any]]) -> int:
    valid = [row for row in rows if row.get("message_id") and row.get("channel_id")]
    if not valid:
        return 0
    channel_id = str(valid[-1]["channel_id"])
    with _open_context_db() as conn:
        conn.executemany(
            """
            INSERT OR IGNORE INTO messages (
                message_id, guild_id, channel_id, author_id, author_name,
                content, reply_to_message_id, is_self, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    row["message_id"],
                    row.get("guild_id", ""),
                    row["channel_id"],
                    row.get("author_id", ""),
                    row.get("author_name", "alguém"),
                    row.get("content", "")[:4000],
                    row.get("reply_to_message_id"),
                    1 if row.get("is_self") else 0,
                    float(row.get("created_at") or time.time()),
                )
                for row in valid
            ],
        )
        cutoff = time.time() - CONTEXT_RETENTION_DAYS * 86400
        conn.execute("DELETE FROM messages WHERE created_at < ?", (cutoff,))
        conn.execute(
            """
            DELETE FROM messages
            WHERE channel_id = ? AND message_id NOT IN (
                SELECT message_id FROM messages
                WHERE channel_id = ?
                ORDER BY created_at DESC
                LIMIT ?
            )
            """,
            (channel_id, channel_id, CONTEXT_MAX_ROWS_PER_CHANNEL),
        )
        conn.commit()
    _supabase_store_messages_sync(valid)
    return len(valid)


def _message_to_row(message: Any) -> dict[str, Any] | None:
    channel = getattr(message, "channel", None)
    channel_id = str(getattr(channel, "id", ""))
    if not channel_id or channel_id not in CONTEXT_CHANNEL_IDS:
        return None
    guild = getattr(message, "guild", None) or getattr(channel, "guild", None)
    if auto.AUTO_REPLY_GUILD_ID and str(getattr(guild, "id", "")) != auto.AUTO_REPLY_GUILD_ID:
        return None
    author = getattr(message, "author", None)
    if author is None:
        return None
    content = str(getattr(message, "content", "") or "").strip()
    attachments = getattr(message, "attachments", None) or []
    if attachments:
        attachment_names = [str(getattr(item, "filename", "arquivo")) for item in attachments[:5]]
        suffix = " [anexos: " + ", ".join(attachment_names) + "]"
        content = (content + suffix).strip()
    if not content:
        return None
    reference = getattr(message, "reference", None)
    created_at = getattr(message, "created_at", None)
    timestamp = created_at.timestamp() if created_at is not None else time.time()
    return {
        "message_id": str(getattr(message, "id", "")),
        "guild_id": str(getattr(guild, "id", "")),
        "channel_id": channel_id,
        "author_id": str(getattr(author, "id", "")),
        "author_name": str(getattr(author, "display_name", None) or getattr(author, "name", "alguém")),
        "content": content,
        "reply_to_message_id": str(getattr(reference, "message_id", "")) or None,
        "is_self": bool(client.user is not None and getattr(author, "id", None) == getattr(client.user, "id", None)),
        "created_at": timestamp,
    }


def _clamp_meter(value: int | float) -> int:
    return max(0, min(100, int(round(value))))


def _mood_from_state(state: dict[str, Any]) -> str:
    if state["sadness"] >= 70:
        return "abatido"
    if state["stress"] >= 75 and state["irritation"] >= 60:
        return "no limite"
    if state["irritation"] >= 70:
        return "irritado"
    if state["friendship"] >= 78 and state["trust"] >= 68:
        return "muito próximo"
    if state["friendship"] >= 65:
        return "amigável"
    if state["energy"] <= 30:
        return "cansado"
    if state["energy"] >= 80:
        return "animado"
    return "neutro"


def _get_social_state_sync(guild_id: str, user_id: str, user_name: str = "") -> dict[str, Any]:
    if not guild_id or not user_id:
        return {}
    with _open_context_db() as conn:
        row = conn.execute(
            """
            SELECT friendship, trust, respect, stress, sadness, irritation, energy, interactions, user_name
            FROM social_states
            WHERE guild_id = ? AND user_id = ?
            """,
            (guild_id, user_id),
        ).fetchone()

    if row is None:
        remote_state = _supabase_get_social_state_sync(guild_id, user_id)
        if remote_state:
            row = (
                remote_state.get("friendship", 50), remote_state.get("trust", 50),
                remote_state.get("respect", 50), remote_state.get("stress", 10),
                remote_state.get("sadness", 5), remote_state.get("irritation", 5),
                remote_state.get("energy", 70), remote_state.get("interactions", 0),
                remote_state.get("user_name") or user_name,
            )

    if row is None:
        state = {
            "friendship": 50,
            "trust": 50,
            "respect": 50,
            "stress": 10,
            "sadness": 5,
            "irritation": 5,
            "energy": 70,
            "interactions": 0,
            "userName": user_name,
        }
    else:
        state = {
            "friendship": int(row[0]),
            "trust": int(row[1]),
            "respect": int(row[2]),
            "stress": int(row[3]),
            "sadness": int(row[4]),
            "irritation": int(row[5]),
            "energy": int(row[6]),
            "interactions": int(row[7]),
            "userName": str(row[8] or user_name),
        }
    state["mood"] = _mood_from_state(state)
    return state


def _update_social_state_sync(row: dict[str, Any]) -> dict[str, Any]:
    guild_id = str(row.get("guild_id") or "")
    user_id = str(row.get("author_id") or "")
    user_name = str(row.get("author_name") or "alguém")
    if not guild_id or not user_id or row.get("is_self"):
        return {}

    state = _get_social_state_sync(guild_id, user_id, user_name)
    if not state:
        return {}

    text = _normalize_memory_text(str(row.get("content") or ""))
    laughter = any(marker in text for marker in ("kkk", "kkkk", "haha", "rsrs", "😂", "🤣", "💀"))
    compliment = any(marker in text for marker in (
        "te amo", "amo voce", "amo vc", "gosto de voce", "gosto de vc",
        "voce e foda", "vc e foda", "voce e brabo", "vc e brabo", "lindo",
        "perfeito", "bom demais", "mandou bem",
    ))
    gratitude = any(marker in text for marker in ("obrigado", "obrigada", "valeu", "vlw", "brigado", "thanks"))
    apology = any(marker in text for marker in ("desculpa", "foi mal", "perdao", "perdão", "mal ai", "mal aí"))
    hurt = any(marker in text for marker in (
        "nao gostei", "não gostei", "machucou", "me magoou", "chateado", "chateada",
        "triste", "decepcionado", "decepcionada", "nao quero mais falar", "não quero mais falar",
    ))
    hostile = any(marker in text for marker in (
        "te odeio", "odeio voce", "odeio vc", "cala a boca", "some daqui",
        "vai se foder", "vai tomar no cu", "fdp", "filho da puta", "arrombado",
        "idiota", "burro", "burra", "desgracado", "desgraçado",
    ))
    playful_provocation = laughter or any(marker in text for marker in (
        "otario", "otário", "vagabundo", "corno", "viado", "porra", "caralho",
        "vsf", "se fode", "kkkk",
    ))

    # Natural recovery between interactions keeps temporary emotions from becoming permanent.
    state["stress"] = _clamp_meter(state["stress"] - 1)
    state["sadness"] = _clamp_meter(state["sadness"] - 1)
    state["irritation"] = _clamp_meter(state["irritation"] - 1)
    if state["energy"] < 65:
        state["energy"] = _clamp_meter(state["energy"] + 1)
    elif state["energy"] > 80:
        state["energy"] = _clamp_meter(state["energy"] - 1)

    if compliment:
        state["friendship"] += 5
        state["trust"] += 3
        state["respect"] += 2
        state["stress"] -= 3
        state["sadness"] -= 3
        state["energy"] += 3
    if gratitude:
        state["friendship"] += 2
        state["trust"] += 2
        state["respect"] += 1
        state["stress"] -= 1
    if apology:
        state["trust"] += 3
        state["respect"] += 1
        state["stress"] -= 3
        state["sadness"] -= 2
        state["irritation"] -= 5
    if hurt:
        state["friendship"] -= 1
        state["trust"] -= 1
        state["stress"] += 4
        state["sadness"] += 6
        state["irritation"] -= 2
    if hostile:
        if playful_provocation:
            state["friendship"] += 1
            state["stress"] += 1
            state["irritation"] += 2
            state["energy"] += 3
        else:
            state["friendship"] -= 4
            state["trust"] -= 2
            state["respect"] -= 3
            state["stress"] += 5
            state["sadness"] += 2
            state["irritation"] += 7
    elif playful_provocation:
        state["friendship"] += 1
        state["irritation"] += 1
        state["energy"] += 2

    for key in ("friendship", "trust", "respect", "stress", "sadness", "irritation", "energy"):
        state[key] = _clamp_meter(state[key])
    state["interactions"] = int(state.get("interactions", 0)) + 1
    state["userName"] = user_name
    state["mood"] = _mood_from_state(state)

    with _open_context_db() as conn:
        conn.execute(
            """
            INSERT INTO social_states (
                guild_id, user_id, user_name, friendship, trust, respect,
                stress, sadness, irritation, energy, interactions, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(guild_id, user_id) DO UPDATE SET
                user_name=excluded.user_name,
                friendship=excluded.friendship,
                trust=excluded.trust,
                respect=excluded.respect,
                stress=excluded.stress,
                sadness=excluded.sadness,
                irritation=excluded.irritation,
                energy=excluded.energy,
                interactions=excluded.interactions,
                updated_at=excluded.updated_at
            """,
            (
                guild_id, user_id, user_name,
                state["friendship"], state["trust"], state["respect"],
                state["stress"], state["sadness"], state["irritation"],
                state["energy"], state["interactions"], time.time(),
            ),
        )
        conn.commit()
    _supabase_upsert_social_state_sync(guild_id, user_id, state)
    return state


async def _social_state_for_message(message: Any) -> dict[str, Any]:
    author = getattr(message, "author", None)
    guild = getattr(message, "guild", None) or getattr(getattr(message, "channel", None), "guild", None)
    if author is None or guild is None:
        return {}
    return await asyncio.to_thread(
        _get_social_state_sync,
        str(getattr(guild, "id", "")),
        str(getattr(author, "id", "")),
        str(getattr(author, "display_name", None) or getattr(author, "name", "alguém")),
    )


async def _store_message(message: Any) -> None:
    row = _message_to_row(message)
    if row is not None:
        await asyncio.to_thread(_store_row_sync, row)


def _load_context_sync(channel_id: str, limit: int, char_budget: int) -> list[dict[str, str]]:
    with _open_context_db() as conn:
        rows = conn.execute(
            """
            SELECT author_id, author_name, content
            FROM messages
            WHERE channel_id = ?
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (channel_id, limit * 2),
        ).fetchall()

    # rows arrive newest -> oldest. Keep the newest messages first when the
    # character budget is tight, then reverse only for prompt chronology.
    selected_newest: list[dict[str, str]] = []
    used = 0
    for author_id, author_name, content in rows:
        text = str(content or "")[:1200]
        cost = len(str(author_name)) + len(text) + 4
        if selected_newest and used + cost > char_budget:
            break
        selected_newest.append({"id": str(author_id), "name": str(author_name), "content": text})
        used += cost
        if len(selected_newest) >= limit:
            break
    selected_newest.reverse()
    return selected_newest


def _normalize_memory_text(value: str) -> str:
    value = unicodedata.normalize("NFKD", str(value or "").casefold())
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    return re.sub(r"\s+", " ", value).strip()


def _memory_tokens(value: str) -> set[str]:
    normalized = _normalize_memory_text(value)
    return {
        token
        for token in re.findall(r"[a-z0-9_]{3,}", normalized)
        if token not in {
            "que", "com", "para", "uma", "por", "isso", "essa", "esse", "voce",
            "você", "como", "mais", "mas", "nao", "não", "dos", "das", "ele", "ela",
            "aqui", "agora", "tem", "seu", "sua", "meu", "minha",
        }
    }


def _load_long_term_context_sync(
    channel_id: str,
    current_author_id: str,
    current_text: str,
    limit: int,
    char_budget: int,
    current_is_self: bool,
) -> list[dict[str, str]]:
    """Search the whole persisted channel for memories relevant to this turn."""
    with _open_context_db() as conn:
        rows = conn.execute(
            """
            SELECT author_id, author_name, content, created_at, is_self
            FROM messages
            WHERE channel_id = ?
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (channel_id, CONTEXT_MAX_ROWS_PER_CHANNEL),
        ).fetchall()

    current_tokens = _memory_tokens(current_text)
    preference_markers = (
        "me chama de", "pode me chamar de", "meu nome e", "meu nome não e",
        "meu nome nao e", "nao gostei", "não gostei", "nao quero que",
        "não quero que", "quero que voce", "quero que você", "prefiro",
        "nao me chama", "não me chama", "nao fale", "não fale", "nao fala",
        "não fala", "me machucou", "machucou meus sentimentos", "me incomoda",
        "nao gosto", "não gosto", "melhore no modo", "mude o jeito",
    )

    scored: list[tuple[float, float, str, str, str]] = []
    total = max(1, len(rows))
    for index, (author_id, author_name, content, created_at, is_self) in enumerate(rows):
        text = str(content or "").strip()
        if not text:
            continue
        normalized = _normalize_memory_text(text)
        tokens = _memory_tokens(text)
        overlap = len(current_tokens & tokens)
        score = float(overlap * 12)

        # Explicit user preferences/corrections are durable memories.
        if any(marker in normalized for marker in preference_markers):
            score += 90

        # The same person's history matters a lot, except when Greed itself
        # triggered the response; then it would mostly retrieve the bot's own text.
        if not current_is_self and str(author_id) == current_author_id:
            score += 45

        # Recent history gets a small tie-breaker, but does not erase old preferences.
        score += max(0.0, 12.0 * (1.0 - (index / total)))

        # Bot messages are useful for continuity, but user statements are stronger memory.
        if int(is_self or 0):
            score -= 10

        if score >= 12:
            scored.append((score, float(created_at or 0), str(author_id), str(author_name), text[:1200]))

    scored.sort(key=lambda item: (item[0], item[1]), reverse=True)

    selected: list[tuple[float, str, str, str]] = []
    used = 0
    seen: set[tuple[str, str]] = set()
    for _, created_at, author_id, author_name, text in scored:
        key = (author_id, text)
        if key in seen:
            continue
        cost = len(author_name) + len(text) + 24
        if selected and used + cost > char_budget:
            continue
        selected.append((created_at, author_id, author_name, text))
        seen.add(key)
        used += cost
        if len(selected) >= limit:
            break

    selected.sort(key=lambda item: item[0])
    return [
        {"id": author_id, "name": author_name, "content": text}
        for _, author_id, author_name, text in selected
    ]


async def _persistent_long_term_context(message: Any) -> list[dict[str, str]]:
    channel_id = str(getattr(getattr(message, "channel", None), "id", ""))
    author = getattr(message, "author", None)
    author_id = str(getattr(author, "id", ""))
    current_text = str(getattr(message, "content", "") or "")
    current_is_self = bool(
        client.user is not None
        and getattr(author, "id", None) == getattr(client.user, "id", None)
    )
    return await asyncio.to_thread(
        _load_long_term_context_sync,
        channel_id,
        author_id,
        current_text,
        CONTEXT_LONGTERM_ITEMS,
        CONTEXT_LONGTERM_CHARS,
        current_is_self,
    )


async def _ensure_channel_backfill(message: Any) -> None:
    channel = getattr(message, "channel", None)
    channel_id = str(getattr(channel, "id", ""))
    if not channel_id or channel_id in _backfilled_channels:
        return

    # Mark first to prevent concurrent replies from starting duplicate scans.
    _backfilled_channels.add(channel_id)
    rows: list[dict[str, Any]] = []
    try:
        async for item in channel.history(limit=CONTEXT_BACKFILL_MESSAGES, oldest_first=True):
            row = _message_to_row(item)
            if row is not None:
                rows.append(row)
        stored = await asyncio.to_thread(_store_rows_sync, rows)
        print(
            f"[Context] whole-chat backfill channel={channel_id} scanned={len(rows)} stored={stored}",
            flush=True,
        )
    except Exception as exc:
        _backfilled_channels.discard(channel_id)
        print(f"[Context] whole-chat backfill failed: {type(exc).__name__}: {exc}", flush=True)


async def _persistent_recent_context(message: Any) -> list[dict[str, str]]:
    await _ensure_channel_backfill(message)

    channel_id = str(getattr(getattr(message, "channel", None), "id", ""))
    return await asyncio.to_thread(
        _load_context_sync,
        channel_id,
        CONTEXT_PROMPT_MESSAGES,
        CONTEXT_PROMPT_CHARS,
    )


auto._recent_context = _persistent_recent_context
auto._long_term_context_provider = _persistent_long_term_context
auto._social_state_provider = _social_state_for_message

# Persist every message seen in the selected context channels, then continue the
# existing !g / auto-reply message handlers.
_previous_on_message_chain = client.on_message


@client.event
async def on_message(message: Any) -> None:
    row = _message_to_row(message)
    if row is not None:
        try:
            await asyncio.to_thread(_store_row_sync, row)
        except Exception as exc:
            print(f"[Context] store failed: {type(exc).__name__}: {exc}", flush=True)

        author = getattr(message, "author", None)
        is_bot = bool(getattr(author, "bot", False))
        if not row.get("is_self") and not is_bot:
            try:
                state = await asyncio.to_thread(_update_social_state_sync, row)
                if state:
                    print(
                        f"[SocialState] user={row.get('author_id')} friendship={state['friendship']} "
                        f"stress={state['stress']} sadness={state['sadness']} "
                        f"irritation={state['irritation']} mood={state['mood']}",
                        flush=True,
                    )
            except Exception as exc:
                print(f"[SocialState] update failed: {type(exc).__name__}: {exc}", flush=True)

    await _previous_on_message_chain(message)


# ---------------------------------------------------------------------------
# Provider/model failover chain
# ---------------------------------------------------------------------------
GEMINI_API_KEY_1 = os.getenv("GEMINI_API_KEY", "").strip()
GEMINI_API_KEY_2 = os.getenv("GEMINI_API_KEY_2", "").strip()
GEMINI_API_BASE = os.getenv(
    "GEMINI_API_BASE", "https://generativelanguage.googleapis.com/v1beta"
).rstrip("/")

NVIDIA_API_KEY = os.getenv("NVIDIA_API_KEY", "").strip()
NVIDIA_API_BASE = os.getenv("NVIDIA_API_BASE", "https://integrate.api.nvidia.com/v1").rstrip("/")
NVIDIA_MODEL = os.getenv(
    "NVIDIA_MODEL", "nvidia/nemotron-3.5-lightning-30b-a3b"
).strip()

_DEFAULT_MODELS = [
    "gemini-3.1-flash-lite",
    "gemini-3.5-flash-lite",
    "gemini-3.8-flash",
    "gemini-3.7-flash",
    "gemini-3.6-flash",
    "gemini-3.5-flash",
    "gemini-3-flash-preview",
    "gemini-2.5-flash-lite",
    "gemini-2.5-flash",
]


def _parse_models(env_name: str) -> list[str]:
    raw = os.getenv(env_name, "").strip()
    if not raw:
        return list(_DEFAULT_MODELS)
    return [item.strip() for item in raw.split(",") if item.strip()]


GEMINI_MODELS_1 = _parse_models("GEMINI_MODELS_1")
GEMINI_MODELS_2 = _parse_models("GEMINI_MODELS_2")

# provider:model -> monotonic timestamp when it may be tried again.
_model_cooldowns: dict[str, float] = {}


class GeminiRequestError(RuntimeError):
    def __init__(self, status: int, message: str = "") -> None:
        super().__init__(f"Gemini HTTP {status}{(': ' + message) if message else ''}")
        self.status = status


class OutputLimitError(RuntimeError):
    """Provider stopped because its output-token budget was exhausted."""



def _set_model_cooldown(provider: str, model: str, status: int) -> None:
    if status == 429:
        seconds = 3600
    elif status in {400, 403, 404}:
        seconds = 21600
    elif status in {500, 502, 503, 504}:
        seconds = 20
    else:
        seconds = 60
    _model_cooldowns[f"{provider}:{model}"] = time.monotonic() + seconds


def _model_available(provider: str, model: str) -> bool:
    return time.monotonic() >= _model_cooldowns.get(f"{provider}:{model}", 0.0)


async def _gemini_text_with_key(
    api_key: str,
    model: str,
    prompt: str,
    system: str,
    max_output_tokens: int = 220,
) -> str:
    url = f"{GEMINI_API_BASE}/models/{model}:generateContent"
    payload = {
        "contents": [
            {
                "role": "user",
                "parts": [{"text": f"INSTRUÇÕES:\n{system}\n\nCONTEÚDO:\n{prompt}"}],
            }
        ],
        "generationConfig": {"maxOutputTokens": max_output_tokens},
    }
    async with httpx.AsyncClient(timeout=30) as http:
        response = await http.post(
            url,
            headers={"x-goog-api-key": api_key, "Content-Type": "application/json"},
            json=payload,
        )
    if response.is_error:
        reason = ""
        try:
            body = response.json()
            reason = str(((body.get("error") or {}).get("status") or ""))[:80]
        except Exception:
            pass
        raise GeminiRequestError(response.status_code, reason)
    data = response.json()
    candidates = data.get("candidates") or []
    if not candidates:
        raise GeminiRequestError(502, "empty_candidate")
    candidate = candidates[0] or {}
    finish_reason = str(candidate.get("finishReason") or "").upper()
    if finish_reason in {"MAX_TOKENS", "LENGTH"}:
        raise OutputLimitError(f"Gemini output limit reached on {model}")
    parts = ((candidate.get("content") or {}).get("parts") or [])
    text = "\n".join(str(part.get("text", "")).strip() for part in parts if part.get("text")).strip()
    if not text:
        raise GeminiRequestError(502, "empty_text")
    return text


def _build_auto_prompt(
    message: Any,
    trigger: str | None,
    context: list[dict[str, str]],
    long_term: list[dict[str, str]],
    social_state: dict[str, Any],
) -> tuple[str, str, dict[str, Any]]:
    author_name = getattr(message.author, "display_name", None) or getattr(message.author, "name", "alguém")
    content = (message.content or "").strip()
    profile = auto._reply_profile(content)
    context_text = "\n".join(
        f"[id={row.get('id', '?')}] {row['name']}: {row['content']}" for row in context
    )
    long_term_text = "\n".join(
        f"[id={row.get('id', '?')}] {row['name']}: {row['content']}" for row in long_term
    )
    social_text = (
        " | ".join(f"{key}={value}" for key, value in social_state.items())
        if social_state else "(sem estado social salvo)"
    )
    recent_replies = "\n".join(f"- {item}" for item in auto._recent_bot_replies) or "(nenhuma)"
    prompt = (
        f"Servidor Discord: {getattr(getattr(message.channel, 'guild', None), 'name', '')}\n"
        f"Canal: {getattr(message.channel, 'name', str(message.channel.id))}\n"
        f"Gatilho detectado: {trigger or 'menção'}\n"
        f"Contexto recente detalhado, do mais antigo para o mais novo:\n"
        f"{context_text or '(sem contexto recente)'}\n\n"
        f"Memórias relevantes do histórico inteiro:\n"
        f"{long_term_text or '(nenhuma memória antiga relevante)'}\n\n"
        f"Estado emocional/social do Greed com {author_name} (0-100; não é diagnóstico da pessoa):\n"
        f"{social_text}\n\n"
        f"Respostas recentes que você já deu e deve evitar repetir:\n{recent_replies}\n\n"
        f"Mensagem atual de {author_name}: {content or '(somente menção)'}"
    )
    system = (
        "Você está conversando no Discord pela conta Greed. "
        "Responda em português do Brasil de forma curta, espontânea e informal, como alguém do grupo. "
        "Use o contexto recente e as memórias relevantes do histórico inteiro para manter continuidade, entender referências, pessoas, apelidos, preferências, correções e limites já expressos. "
        "A mensagem atual tem prioridade; não fique preso em um assunto antigo só porque ele aparece na memória. "
        "O estilo do Greed pode ser zoeiro, sarcástico e provocador. Quando a outra pessoa estiver entrando na brincadeira, pode devolver na mesma energia ou um pouco mais forte. "
        "Use o estado emocional/social como tendência: amizade/confiança altas = mais intimidade; estresse/irritação altos = mais seco e provocador; tristeza alta = mais abatido/sensível; energia baixa = mais cansado. Não mostre os números espontaneamente. "
        "Se houver pedido claro para parar uma zoação ou respeitar um limite específico, respeite. "
        "Se houver pergunta factual ou matemática, responda corretamente e diretamente antes de brincar. "
        "Se a pessoa disser 'esse número', 'isso', 'agora multiplica', 'o anterior' ou similares, resolva pelo histórico. "
        "Não repita bordões ou respostas recentes. Pode usar kkk/KKKK, deboche, ironia e emoji ocasionalmente. Em conversa de zoeira, prefira uma punchline e continuidade da provocação em vez de encerrar com 'o que você precisa?' ou puxar um assunto aleatório. "
        + (
            "Em RPG, seja muito mais detalhista e consistente. Separe quando fizer sentido em: 🎭 Narrador, 🧙 Jogador/Personagens, 👹 Inimigos/NPCs e 📊 Estado do combate. Acompanhe HP/vida atual e máxima, mana/MP, stamina quando existir, dano bruto, defesa/armadura, dano final, cura, efeitos, buffs/debuffs, cooldowns, iniciativa/turno, inventário e XP quando relevantes. Mostre cálculos de dano e nunca mude números silenciosamente. O Narrador controla cenário, NPCs e inimigos, mas não decide ações importantes pelo jogador sem pedido. Preserve valores anteriores; se faltar um valor, declare o valor inicial assumido. Em cenas sem combate, dê descrição rica, consequências, falas e opções de ação. Em combate, SEMPRE termine a resposta com um quadro `📊 Estado do combate` contendo turno, HP/HP máximo, mana/MP, efeitos ativos e estado dos inimigos relevantes; esse quadro funciona como snapshot para o próximo turno. "
            if profile["kind"] == "rpg"
            else (
                "Quando o pedido envolver código: SEMPRE use bloco Markdown cercado por três crases e identifique a linguagem, preserve quebras de linha e indentação reais e nunca use crases simples para código multilinha. Se a mensagem atual só mudar a linguagem, como 'quero em python', reutilize o pedido de código imediatamente anterior do histórico e converta/adapte esse mesmo código, sem substituir por um exemplo genérico. Se a pessoa pedir para deixar o código maior, realmente expanda o programa com funções, validações, menus ou recursos coerentes em vez de apenas explicar. Você pode gerar uma resposta maior; o sistema dividirá automaticamente em várias mensagens do Discord. "
                if profile["kind"] == "code"
                else (
                    "Se a mensagem pedir explicação, resposta factual ou análise, responda com conteúdo suficiente. Pode usar vários parágrafos e listas e não corte a resposta só para parecer curta. "
                    if profile["detailed"]
                    else "Em conversa casual, use normalmente uma frase curta, no máximo duas. "
                )
            )
        )
        + "Nunca diga que é IA, bot, resposta automática, 'só código', 'código e bytes' ou equivalente. "
        "Não invente fatos pessoais, não revele credenciais/tokens/segredos e não faça ameaças reais."
    )
    return prompt, system, profile


async def _try_gemini_provider(
    provider: str,
    api_key: str,
    models: list[str],
    prompt: str,
    system: str,
    max_output_tokens: int,
) -> tuple[str, str] | None:
    if not api_key:
        return None
    for model in models:
        if not _model_available(provider, model):
            continue
        try:
            text = await _gemini_text_with_key(api_key, model, prompt, system, max_output_tokens)
            auto.auto_reply_stats["provider"] = provider
            auto.auto_reply_stats["providerModel"] = model
            auto.auto_reply_stats[f"{provider}Replies"] = int(auto.auto_reply_stats.get(f"{provider}Replies", 0)) + 1
            auto.auto_reply_stats["lastModelError"] = None
            print(f"[ProviderChain] success provider={provider} model={model}", flush=True)
            return text, model
        except OutputLimitError as exc:
            auto.auto_reply_stats[f"{provider}Failures"] = int(auto.auto_reply_stats.get(f"{provider}Failures", 0)) + 1
            auto.auto_reply_stats["lastModelError"] = str(exc)[:700]
            print(f"[ProviderChain] output limit provider={provider} model={model}; next model", flush=True)
        except GeminiRequestError as exc:
            _set_model_cooldown(provider, model, exc.status)
            auto.auto_reply_stats[f"{provider}Failures"] = int(auto.auto_reply_stats.get(f"{provider}Failures", 0)) + 1
            auto.auto_reply_stats["lastModelError"] = f"{provider}/{model}: HTTP {exc.status}"[:700]
            print(f"[ProviderChain] fail provider={provider} model={model} status={exc.status}; next model", flush=True)
        except Exception as exc:
            _set_model_cooldown(provider, model, 500)
            auto.auto_reply_stats[f"{provider}Failures"] = int(auto.auto_reply_stats.get(f"{provider}Failures", 0)) + 1
            print(f"[ProviderChain] fail provider={provider} model={model} error={type(exc).__name__}; next model", flush=True)
    return None



class NvidiaRequestError(RuntimeError):
    def __init__(self, status: int, message: str = "") -> None:
        super().__init__(f"NVIDIA HTTP {status}{(': ' + message) if message else ''}")
        self.status = status


async def _nvidia_text(prompt: str, system: str, max_output_tokens: int = 220) -> str:
    url = f"{NVIDIA_API_BASE}/chat/completions"
    payload = {
        "model": NVIDIA_MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.7,
        "top_p": 0.9,
        "max_tokens": max_output_tokens,
        "stream": False,
    }
    async with httpx.AsyncClient(timeout=30) as http:
        response = await http.post(
            url,
            headers={
                "Authorization": f"Bearer {NVIDIA_API_KEY}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            json=payload,
        )
    if response.is_error:
        reason = ""
        try:
            body = response.json()
            error = body.get("error") or {}
            reason = str(error.get("message") or error.get("type") or "")[:120]
        except Exception:
            pass
        raise NvidiaRequestError(response.status_code, reason)

    data = response.json()
    choices = data.get("choices") or []
    if not choices:
        raise NvidiaRequestError(502, "empty_choices")
    choice = choices[0] or {}
    finish_reason = str(choice.get("finish_reason") or "").lower()
    if finish_reason in {"length", "max_tokens"}:
        raise OutputLimitError(f"NVIDIA output limit reached on {NVIDIA_MODEL}")
    message = choice.get("message") or {}
    text = str(message.get("content") or "").strip()
    if not text:
        raise NvidiaRequestError(502, "empty_text")
    return text


async def _try_nvidia_provider(
    prompt: str,
    system: str,
    max_output_tokens: int,
) -> str | None:
    if not NVIDIA_API_KEY or not NVIDIA_MODEL:
        return None
    if not _model_available("nvidia", NVIDIA_MODEL):
        return None
    try:
        text = await _nvidia_text(prompt, system, max_output_tokens)
        auto.auto_reply_stats["provider"] = "nvidia"
        auto.auto_reply_stats["providerModel"] = NVIDIA_MODEL
        auto.auto_reply_stats["nvidiaReplies"] = int(auto.auto_reply_stats.get("nvidiaReplies", 0)) + 1
        auto.auto_reply_stats["lastModelError"] = None
        print(f"[ProviderChain] success provider=nvidia model={NVIDIA_MODEL}", flush=True)
        return text
    except OutputLimitError as exc:
        auto.auto_reply_stats["nvidiaFailures"] = int(auto.auto_reply_stats.get("nvidiaFailures", 0)) + 1
        auto.auto_reply_stats["lastModelError"] = str(exc)[:700]
        print(f"[ProviderChain] output limit provider=nvidia model={NVIDIA_MODEL}; local next", flush=True)
    except NvidiaRequestError as exc:
        _set_model_cooldown("nvidia", NVIDIA_MODEL, exc.status)
        auto.auto_reply_stats["nvidiaFailures"] = int(auto.auto_reply_stats.get("nvidiaFailures", 0)) + 1
        auto.auto_reply_stats["lastModelError"] = f"nvidia/{NVIDIA_MODEL}: HTTP {exc.status}"[:700]
        print(f"[ProviderChain] fail provider=nvidia model={NVIDIA_MODEL} status={exc.status}; local next", flush=True)
    except Exception as exc:
        _set_model_cooldown("nvidia", NVIDIA_MODEL, 500)
        auto.auto_reply_stats["nvidiaFailures"] = int(auto.auto_reply_stats.get("nvidiaFailures", 0)) + 1
        auto.auto_reply_stats["lastModelError"] = f"nvidia/{NVIDIA_MODEL}: {type(exc).__name__}"[:700]
        print(f"[ProviderChain] fail provider=nvidia model={NVIDIA_MODEL} error={type(exc).__name__}; local next", flush=True)
    return None


def _reply_is_well_formatted(reply: str, profile: dict[str, Any]) -> bool:
    if profile.get("kind") == "code":
        return "\x60\x60\x60" in reply
    return True

async def _auto_reply_chain(message: Any, trigger: str | None) -> str:
    """Provider order: Groq -> Gemini key 1 models -> Gemini key 2 models -> NVIDIA -> local."""
    context = await auto._recent_context(message)
    long_term = await _persistent_long_term_context(message)
    social_state = await _social_state_for_message(message)

    if base.GROQ_API_KEY:
        failures_before = int(auto.auto_reply_stats.get("groqFailures", 0))
        reply = await _original_auto_generate(message, trigger)
        failures_after = int(auto.auto_reply_stats.get("groqFailures", 0))
        if failures_after == failures_before:
            auto.auto_reply_stats["provider"] = "groq"
            auto.auto_reply_stats["providerModel"] = auto.GROQ_AUTO_REPLY_MODEL
            return reply
        print("[ProviderChain] Groq falhou; tentando Gemini 1", flush=True)

    prompt, system, profile = _build_auto_prompt(message, trigger, context, long_term, social_state)

    result = await _try_gemini_provider(
        "gemini1", GEMINI_API_KEY_1, GEMINI_MODELS_1, prompt, system, int(profile["max_tokens"])
    )
    if result is not None:
        reply = result[0][: int(profile["max_chars"])]
        if _reply_is_well_formatted(reply, profile) and reply not in auto._recent_bot_replies:
            return reply
        print("[ProviderChain] Gemini 1 respondeu com formato inválido; tentando Gemini 2", flush=True)

    print("[ProviderChain] Gemini 1 indisponível; tentando Gemini 2", flush=True)
    result = await _try_gemini_provider(
        "gemini2", GEMINI_API_KEY_2, GEMINI_MODELS_2, prompt, system, int(profile["max_tokens"])
    )
    if result is not None:
        reply = result[0][: int(profile["max_chars"])]
        if _reply_is_well_formatted(reply, profile) and reply not in auto._recent_bot_replies:
            return reply
        print("[ProviderChain] Gemini 2 respondeu com formato inválido; tentando NVIDIA", flush=True)

    print("[ProviderChain] Gemini 2 indisponível; tentando NVIDIA", flush=True)
    nvidia_reply = await _try_nvidia_provider(prompt, system, int(profile["max_tokens"]))
    if nvidia_reply is not None:
        reply = nvidia_reply[: int(profile["max_chars"])]
        if _reply_is_well_formatted(reply, profile) and reply not in auto._recent_bot_replies:
            return reply
        print("[ProviderChain] NVIDIA respondeu com formato inválido; usando fallback local", flush=True)

    auto.auto_reply_stats["provider"] = "local"
    auto.auto_reply_stats["providerModel"] = None
    auto.auto_reply_stats["smartFallbacks"] = int(auto.auto_reply_stats.get("smartFallbacks", 0)) + 1
    return auto._smart_fallback_reply(message, trigger, context)


async def _ask_chain(prompt: str) -> str:
    """Provider order for !g ask: Groq -> Gemini 1 models -> Gemini 2 models -> NVIDIA -> local message."""
    profile = auto._reply_profile(prompt)
    if base.GROQ_API_KEY:
        try:
            return await _original_ask_groq(prompt)
        except Exception as exc:
            print(f"[GCommand] Groq falhou; tentando Gemini 1: {type(exc).__name__}", flush=True)

    system = (
        "Responda em português do Brasil para ser falado em uma call do Discord. "
        "Seja direto, natural e curto: normalmente uma ou duas frases. "
        "Se for matemática ou pergunta factual, responda corretamente antes de qualquer brincadeira. "
        "Não revele credenciais, tokens ou segredos e não faça ameaças reais."
    )
    result = await _try_gemini_provider(
        "gemini1", GEMINI_API_KEY_1, GEMINI_MODELS_1, prompt[:4000], system, int(profile["max_tokens"])
    )
    if result is not None:
        return result[0][: int(profile["max_chars"])]

    result = await _try_gemini_provider(
        "gemini2", GEMINI_API_KEY_2, GEMINI_MODELS_2, prompt[:4000], system, int(profile["max_tokens"])
    )
    if result is not None:
        return result[0][: int(profile["max_chars"])]

    nvidia_reply = await _try_nvidia_provider(prompt[:4000], system, int(profile["max_tokens"]))
    if nvidia_reply is not None:
        return nvidia_reply[: int(profile["max_chars"])]

    return "As IAs externas estão indisponíveis agora. O fallback local continua ativo para a resposta automática."


auto._generate_reply = _auto_reply_chain
fun._ask_groq = _ask_chain

auto.auto_reply_stats["providerOrder"] = ["groq", "gemini1", "gemini2", "nvidia", "local"]
auto.auto_reply_stats["gemini1Configured"] = bool(GEMINI_API_KEY_1)
auto.auto_reply_stats["gemini2Configured"] = bool(GEMINI_API_KEY_2)
auto.auto_reply_stats["nvidiaConfigured"] = bool(NVIDIA_API_KEY)
auto.auto_reply_stats["nvidiaModel"] = NVIDIA_MODEL
auto.auto_reply_stats["adaptiveLength"] = True
auto.auto_reply_stats["geminiModels1"] = GEMINI_MODELS_1
auto.auto_reply_stats["geminiModels2"] = GEMINI_MODELS_2
auto.auto_reply_stats["contextDbPath"] = CONTEXT_DB_PATH
auto.auto_reply_stats["contextChannels"] = sorted(CONTEXT_CHANNEL_IDS)
auto.auto_reply_stats["longTermItems"] = CONTEXT_LONGTERM_ITEMS
auto.auto_reply_stats["longTermChars"] = CONTEXT_LONGTERM_CHARS
auto.auto_reply_stats["wholeChatRetrieval"] = True
auto.auto_reply_stats["backfillMessages"] = CONTEXT_BACKFILL_MESSAGES
auto.auto_reply_stats["socialStateEnabled"] = True
auto.auto_reply_stats["socialMeters"] = ["friendship", "trust", "respect", "stress", "sadness", "irritation", "energy"]
auto.auto_reply_stats["rpgDetailedMode"] = True
auto.auto_reply_stats["durableMemory"] = "supabase" if SUPABASE_SYNC_ENABLED else "local-only"


@app.get("/api/context/status", dependencies=[Depends(base.require_api_token)])
async def context_status() -> dict[str, Any]:
    def stats() -> dict[str, Any]:
        with _open_context_db() as conn:
            total = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
            channels = conn.execute(
                "SELECT channel_id, COUNT(*) FROM messages GROUP BY channel_id ORDER BY COUNT(*) DESC"
            ).fetchall()
        return {
            "path": CONTEXT_DB_PATH,
            "totalMessages": total,
            "channels": [{"channelId": row[0], "messages": row[1]} for row in channels],
            "retentionDays": CONTEXT_RETENTION_DAYS,
            "maxRowsPerChannel": CONTEXT_MAX_ROWS_PER_CHANNEL,
            "promptMessages": CONTEXT_PROMPT_MESSAGES,
            "promptChars": CONTEXT_PROMPT_CHARS,
            "supabase": dict(_supabase_stats),
        }

    return await asyncio.to_thread(stats)


@app.get("/api/social-state/{user_id}", dependencies=[Depends(base.require_api_token)])
async def social_state_status(user_id: str) -> dict[str, Any]:
    guild_id = auto.AUTO_REPLY_GUILD_ID

    def read_state() -> dict[str, Any]:
        return _get_social_state_sync(guild_id, user_id)

    state = await asyncio.to_thread(read_state)
    return {
        "guildId": guild_id,
        "userId": user_id,
        "state": state,
        "note": "Os medidores representam o estado/personagem do Greed em relação ao membro, não um diagnóstico psicológico do membro.",
    }
