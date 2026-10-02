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

_default_context_channels = [
    os.getenv("AUTO_REPLY_CHANNEL_ID", "1554920683786739712").strip(),
    os.getenv("G_COMMAND_CHANNEL_ID", "1554920170659774545").strip(),
]
CONTEXT_CHANNEL_IDS = {
    item.strip()
    for item in os.getenv("CONTEXT_CHANNEL_IDS", ",".join(_default_context_channels)).split(",")
    if item.strip()
}


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

# Persist every message seen in the selected context channels, then continue the
# existing !g / auto-reply message handlers.
_previous_on_message_chain = client.on_message


@client.event
async def on_message(message: Any) -> None:
    try:
        await _store_message(message)
    except Exception as exc:
        print(f"[Context] store failed: {type(exc).__name__}: {exc}", flush=True)
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
) -> tuple[str, str, dict[str, Any]]:
    author_name = getattr(message.author, "display_name", None) or getattr(message.author, "name", "alguém")
    content = (message.content or "").strip()
    profile = auto._reply_profile(content)
    context_text = "\n".join(f"{row['name']}: {row['content']}" for row in context)
    recent_replies = "\n".join(f"- {item}" for item in auto._recent_bot_replies) or "(nenhuma)"
    prompt = (
        f"Servidor Discord: {getattr(getattr(message.channel, 'guild', None), 'name', '')}\n"
        f"Canal: {getattr(message.channel, 'name', str(message.channel.id))}\n"
        f"Gatilho detectado: {trigger or 'menção'}\n"
        f"Histórico persistido da conversa, do mais antigo para o mais novo:\n"
        f"{context_text or '(sem contexto)'}\n\n"
        f"Respostas recentes que você já deu e deve evitar repetir:\n{recent_replies}\n\n"
        f"Mensagem atual de {author_name}: {content or '(somente menção)'}"
    )
    system = (
        "Você está conversando no Discord pela conta Greed. "
        "Responda em português do Brasil de forma curta, espontânea e informal, como alguém do grupo. "
        "Use o contexto recente e as memórias relevantes do histórico inteiro para manter continuidade, entender referências, pessoas, apelidos, preferências, correções e limites já expressos. "
        "A mensagem atual tem prioridade; não fique preso em um assunto antigo só porque ele aparece na memória. "
        "Quando alguém disser que não gostou de uma brincadeira, que algo machucou, ou pedir mudança no modo de falar, trate isso como preferência duradoura daquela mesma pessoa e não repita o padrão ofensivo depois. "
        "Se houver pergunta factual ou matemática, responda corretamente e diretamente antes de brincar. "
        "Se a pessoa disser 'esse número', 'isso', 'agora multiplica', 'o anterior' ou similares, resolva pelo histórico. "
        "Não repita bordões ou respostas recentes. Pode usar kkk/KKKK e emoji ocasionalmente, sem exagerar. "
        + (
            "Quando o pedido envolver código: SEMPRE use bloco Markdown cercado por três crases e identifique a linguagem, preserve quebras de linha e indentação reais e nunca use crases simples para código multilinha. Se a mensagem atual só mudar a linguagem, como 'quero em python', reutilize o pedido de código imediatamente anterior do histórico e converta/adapte esse mesmo código, sem substituir por um exemplo genérico. Se a pessoa pedir para deixar o código maior, realmente expanda o programa com funções, validações, menus ou recursos coerentes em vez de apenas explicar. Você pode gerar uma resposta maior; o sistema dividirá automaticamente em várias mensagens do Discord. "
            if profile["kind"] == "code"
            else (
                "Se a mensagem pedir explicação, resposta factual ou análise, responda com conteúdo suficiente. Pode usar vários parágrafos e listas e não corte a resposta só para parecer curta. "
                if profile["detailed"]
                else "Em conversa casual, use normalmente uma frase curta, no máximo duas. "
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

    if base.GROQ_API_KEY:
        failures_before = int(auto.auto_reply_stats.get("groqFailures", 0))
        reply = await _original_auto_generate(message, trigger)
        failures_after = int(auto.auto_reply_stats.get("groqFailures", 0))
        if failures_after == failures_before:
            auto.auto_reply_stats["provider"] = "groq"
            auto.auto_reply_stats["providerModel"] = auto.GROQ_AUTO_REPLY_MODEL
            return reply
        print("[ProviderChain] Groq falhou; tentando Gemini 1", flush=True)

    prompt, system, profile = _build_auto_prompt(message, trigger, context, long_term)

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
        }

    return await asyncio.to_thread(stats)
