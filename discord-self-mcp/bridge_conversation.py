import asyncio
import io
import os
import re
import time
import wave
from collections import defaultdict, deque
from typing import Any

import httpx
from fastapi import Depends, HTTPException

import bridge_join_announce as stack

app = stack.app
auto = stack.auto
voice = stack.voice
base = auto.base
client = auto.client

try:
    from discord.ext.native_voice import BasicSink, ConditionalFilter, PCMDecodeSink
    from discord.ext.native_voice import VoiceClient as NativeVoiceClient

    NATIVE_VOICE_AVAILABLE = True
    NATIVE_VOICE_ERROR: str | None = None
except Exception as exc:  # keep text/TTS bridge alive if the optional receiver fails to import
    BasicSink = ConditionalFilter = PCMDecodeSink = NativeVoiceClient = None  # type: ignore
    NATIVE_VOICE_AVAILABLE = False
    NATIVE_VOICE_ERROR = f"{type(exc).__name__}: {exc}"

CONVERSATION_GUILD_ID = os.getenv("CONVERSATION_GUILD_ID", "1251266361569710222").strip()
TRANSCRIPTION_MODEL = os.getenv("GROQ_TRANSCRIPTION_MODEL", "whisper-large-v3-turbo").strip()
CONVERSATION_MODEL = os.getenv("GROQ_CONVERSATION_MODEL", "openai/gpt-oss-120b").strip()
CONSENT_COMMAND = os.getenv("VOICE_CONSENT_COMMAND", "!consentir").strip() or "!consentir"
REVOKE_COMMAND = os.getenv("VOICE_REVOKE_COMMAND", "!revogar").strip() or "!revogar"
SILENCE_END_SECONDS = max(0.35, min(2.0, float(os.getenv("VOICE_SILENCE_END_SECONDS", "0.70"))))
MIN_UTTERANCE_SECONDS = max(0.20, min(3.0, float(os.getenv("VOICE_MIN_UTTERANCE_SECONDS", "0.55"))))
MAX_UTTERANCE_SECONDS = max(3.0, min(30.0, float(os.getenv("VOICE_MAX_UTTERANCE_SECONDS", "18"))))
AI_MIN_INTERVAL_SECONDS = max(0.5, min(10.0, float(os.getenv("VOICE_AI_MIN_INTERVAL_SECONDS", "1.8"))))
EVENT_LIMIT = max(30, min(500, int(os.getenv("VOICE_EVENT_LIMIT", "220"))))

# Discord Opus decode output: 48 kHz, signed 16-bit stereo PCM.
PCM_SAMPLE_RATE = 48_000
PCM_CHANNELS = 2
PCM_SAMPLE_WIDTH = 2
PCM_BYTES_PER_SECOND = PCM_SAMPLE_RATE * PCM_CHANNELS * PCM_SAMPLE_WIDTH

conversation_mode = "off"  # off | transcribe | interactive
conversation_running = False
conversation_thinking = False
conversation_channel_id: str | None = None
current_speaker: dict[str, Any] | None = None

consented_users: dict[int, dict[str, Any]] = {}
recent_events: deque[dict[str, Any]] = deque(maxlen=EVENT_LIMIT)
conversation_history: deque[dict[str, Any]] = deque(maxlen=36)
active_speakers: dict[int, float] = {}

_audio_queue: asyncio.Queue[tuple[int, bytes | None, float, bool]] = asyncio.Queue(maxsize=2500)
_audio_buffers: dict[int, bytearray] = defaultdict(bytearray)
_audio_started_at: dict[int, float] = {}
_audio_last_speech_at: dict[int, float] = {}
_audio_worker_task: asyncio.Task | None = None
_housekeeping_task: asyncio.Task | None = None
_transcription_tasks: dict[int, set[asyncio.Task]] = defaultdict(set)
_transcription_sem: asyncio.Semaphore | None = None
_ai_lock: asyncio.Lock | None = None
_last_ai_decision_at = 0.0
_listener_sink: Any | None = None
_event_loop: asyncio.AbstractEventLoop | None = None
_event_id = 0

_original_ensure_voice_channel = voice.ensure_voice_channel
_original_on_message = auto.on_message


def _emit(kind: str, text: str, *, user_id: int | None = None, speaker: str | None = None, **extra: Any) -> None:
    global _event_id
    _event_id += 1
    item: dict[str, Any] = {
        "id": _event_id,
        "ts": time.time(),
        "type": kind,
        "text": text,
    }
    if user_id is not None:
        item["userId"] = str(user_id)
    if speaker:
        item["speaker"] = speaker
    item.update(extra)
    recent_events.append(item)


def _display_name(user_id: int) -> str:
    try:
        guild = client.get_guild(int(CONVERSATION_GUILD_ID)) if CONVERSATION_GUILD_ID else None
        member = guild.get_member(user_id) if guild is not None else None
        if member is not None:
            return str(getattr(member, "display_name", None) or getattr(member, "name", user_id))
        user = client.get_user(user_id)
        if user is not None:
            return str(getattr(user, "display_name", None) or getattr(user, "name", user_id))
    except Exception:
        pass
    return str(user_id)


def _active_channel_members() -> list[Any]:
    vc = voice.active_voice_client
    channel = getattr(vc, "channel", None) if vc else None
    members = getattr(channel, "members", None) if channel else None
    return list(members or [])


def _same_active_call(user_id: int) -> bool:
    return any(int(getattr(member, "id", 0)) == int(user_id) for member in _active_channel_members())


def _participant_payload() -> list[dict[str, Any]]:
    now = time.monotonic()
    rows: list[dict[str, Any]] = []
    for member in _active_channel_members():
        uid = int(getattr(member, "id", 0) or 0)
        if not uid:
            continue
        rows.append(
            {
                "userId": str(uid),
                "name": str(getattr(member, "display_name", None) or getattr(member, "name", uid)),
                "consented": uid in consented_users,
                "speaking": uid in consented_users and now - active_speakers.get(uid, 0.0) < 0.9,
                "self": bool(client.user and uid == int(client.user.id)),
            }
        )
    rows.sort(key=lambda row: (not row["consented"], row["name"].casefold()))
    return rows


def _clear_user_audio(user_id: int, *, cancel_tasks: bool = False) -> None:
    _audio_buffers.pop(user_id, None)
    _audio_started_at.pop(user_id, None)
    _audio_last_speech_at.pop(user_id, None)
    active_speakers.pop(user_id, None)
    if cancel_tasks:
        for task in list(_transcription_tasks.get(user_id, ())):
            if not task.done():
                task.cancel()
        _transcription_tasks.pop(user_id, None)


def _clear_all_audio(*, cancel_tasks: bool = False) -> None:
    for uid in set(_audio_buffers) | set(_transcription_tasks):
        _clear_user_audio(uid, cancel_tasks=cancel_tasks)
    while not _audio_queue.empty():
        try:
            _audio_queue.get_nowait()
            _audio_queue.task_done()
        except (asyncio.QueueEmpty, ValueError):
            break


async def ensure_native_voice_channel(channel_id: str):
    """Use native receive-capable voice transport while keeping the existing TTS/music API."""
    if not NATIVE_VOICE_AVAILABLE:
        return await _original_ensure_voice_channel(channel_id)

    channel = await voice.resolve_voice_channel(channel_id)
    vc = voice.active_voice_client

    if vc is not None and getattr(vc, "is_connected", lambda: False)():
        current = getattr(vc, "channel", None)
        if current is not None and int(current.id) == int(channel.id):
            if isinstance(vc, NativeVoiceClient):
                return vc, channel
            await voice.disconnect_voice()
            vc = None
        else:
            current_guild = getattr(current, "guild", None)
            target_guild = getattr(channel, "guild", None)
            if isinstance(vc, NativeVoiceClient) and current_guild is not None and target_guild is not None and current_guild.id == target_guild.id:
                try:
                    await vc.move_to(channel)
                    return vc, channel
                except Exception as exc:
                    raise HTTPException(status_code=400, detail=f"Unable to move voice channel: {exc}") from exc
            await voice.disconnect_voice()
            vc = None

    try:
        voice.active_voice_client = await channel.connect(
            cls=NativeVoiceClient,
            timeout=30,
            reconnect=True,
            self_deaf=False,
            self_mute=False,
        )
        return voice.active_voice_client, channel
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Unable to join native voice channel: {type(exc).__name__}: {exc}") from exc


# Every existing /join, TTS, soundboard and music call now gets the receive-capable client.
voice.ensure_voice_channel = ensure_native_voice_channel


def _consent_predicate(packet: Any) -> bool:
    if not conversation_running or conversation_mode == "off":
        return False
    uid = getattr(packet, "user_id", None)
    if uid is None:
        return False
    return int(uid) in consented_users


def _enqueue_packet(item: tuple[int, bytes | None, float, bool]) -> None:
    try:
        _audio_queue.put_nowait(item)
    except asyncio.QueueFull:
        try:
            _audio_queue.get_nowait()
            _audio_queue.task_done()
        except (asyncio.QueueEmpty, ValueError):
            pass
        try:
            _audio_queue.put_nowait(item)
        except asyncio.QueueFull:
            pass


def _on_pcm_packet(packet: Any) -> None:
    loop = _event_loop
    if loop is None or not conversation_running:
        return
    uid_value = getattr(packet, "user_id", None)
    if uid_value is None:
        return
    uid = int(uid_value)
    if uid not in consented_users:
        return

    payload = bytes(getattr(packet, "payload", b"") or b"")
    if not payload:
        return

    voice_activity = getattr(packet, "audio_voice_activity", None)
    audio_level = getattr(packet, "audio_level", None)
    # Discord RTP level 127 is silence. Keep a margin for room noise.
    is_speech = voice_activity is not False and (audio_level is None or int(audio_level) < 120)
    item = (uid, payload if is_speech else None, time.monotonic(), is_speech)
    try:
        loop.call_soon_threadsafe(_enqueue_packet, item)
    except RuntimeError:
        return


def _listener_after(error: Exception | None) -> None:
    loop = _event_loop
    if loop is None:
        return

    def settle() -> None:
        if error is not None:
            _emit("error", f"Recepção de voz encerrada: {type(error).__name__}: {error}")

    try:
        loop.call_soon_threadsafe(settle)
    except RuntimeError:
        pass


def _wav_bytes(pcm: bytes) -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setnchannels(PCM_CHANNELS)
        wav.setsampwidth(PCM_SAMPLE_WIDTH)
        wav.setframerate(PCM_SAMPLE_RATE)
        wav.writeframes(pcm)
    return output.getvalue()


async def _transcribe_pcm(user_id: int, pcm: bytes) -> str:
    if user_id not in consented_users or not conversation_running:
        return ""
    if not base.GROQ_API_KEY:
        raise RuntimeError("GROQ_API_KEY não configurada")

    wav_data = _wav_bytes(pcm)
    files = {"file": ("fala.wav", wav_data, "audio/wav")}
    data = {
        "model": TRANSCRIPTION_MODEL,
        "language": "pt",
        "response_format": "json",
        "temperature": "0",
    }
    headers = {"Authorization": f"Bearer {base.GROQ_API_KEY}"}
    async with httpx.AsyncClient(timeout=35) as http:
        response = await http.post(
            "https://api.groq.com/openai/v1/audio/transcriptions",
            headers=headers,
            data=data,
            files=files,
        )
    if response.is_error:
        detail = response.text.replace("\n", " ")[:500]
        raise RuntimeError(f"Whisper HTTP {response.status_code}: {detail}")
    text = re.sub(r"\s+", " ", str(response.json().get("text", ""))).strip()
    return text[:2500]


async def _maybe_ai_response(user_id: int, speaker: str, transcript: str) -> None:
    global conversation_thinking, _last_ai_decision_at
    if conversation_mode != "interactive" or not conversation_running or user_id not in consented_users:
        return
    if not base.GROQ_API_KEY:
        return

    now = time.monotonic()
    if now - _last_ai_decision_at < AI_MIN_INTERVAL_SECONDS:
        return

    assert _ai_lock is not None
    async with _ai_lock:
        now = time.monotonic()
        if now - _last_ai_decision_at < AI_MIN_INTERVAL_SECONDS:
            return
        _last_ai_decision_at = now
        conversation_thinking = True
        _emit("thinking", f"Analisando a fala de {speaker}…", user_id=user_id, speaker=speaker)

        history_lines = []
        for item in list(conversation_history)[-18:]:
            role = item.get("role")
            name = item.get("speaker") or ("Greed" if role == "assistant" else "Pessoa")
            history_lines.append(f"{name}: {item.get('text', '')}")

        system_prompt = (
            "Você participa por voz de uma call privada do Discord como Greed. Responda em português do Brasil, de forma curta, natural, informal e divertida quando couber. "
            "Você NÃO deve interromper toda fala. Responda quando alguém claramente falar com Greed, fizer uma pergunta que comporta resposta, pedir sua opinião/ajuda, ou quando uma resposta curta acrescentaria algo natural à conversa. "
            "Para comentários soltos, risadas, conversa entre outras pessoas ou quando seria estranho interromper, responda exatamente NO_REPLY. "
            "Se responder, devolva APENAS a frase que deve ser falada, normalmente uma ou duas frases curtas. "
            "Não revele credenciais/segredos, não invente fatos pessoais e não faça ameaças reais."
        )
        user_prompt = (
            "Contexto recente da call:\n"
            + ("\n".join(history_lines) if history_lines else "(sem histórico)")
            + f"\n\nÚltima fala de {speaker}: {transcript}\n\nDecida se Greed deve responder agora."
        )

        try:
            async with httpx.AsyncClient(timeout=30) as http:
                response = await http.post(
                    "https://api.groq.com/openai/v1/chat/completions",
                    headers={"Authorization": f"Bearer {base.GROQ_API_KEY}", "Content-Type": "application/json"},
                    json={
                        "model": CONVERSATION_MODEL,
                        "temperature": 0.85,
                        "max_completion_tokens": 150,
                        "messages": [
                            {"role": "system", "content": system_prompt},
                            {"role": "user", "content": user_prompt},
                        ],
                    },
                )
            if response.is_error:
                detail = response.text.replace("\n", " ")[:500]
                raise RuntimeError(f"Groq HTTP {response.status_code}: {detail}")
            reply = str(response.json()["choices"][0]["message"]["content"]).strip()
            reply = re.sub(r"\s+", " ", reply)
            if not reply or reply.casefold().startswith("no_reply"):
                return
            reply = reply[:600]
            if not conversation_running or conversation_mode != "interactive":
                return
            conversation_history.append({"role": "assistant", "speaker": "Greed", "text": reply, "ts": time.time()})
            _emit("response", reply, speaker="Greed")
            channel_id = conversation_channel_id
            if channel_id:
                await voice.speak_robot(channel_id, reply)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            _emit("error", f"Falha na resposta da IA: {type(exc).__name__}: {exc}")
        finally:
            conversation_thinking = False


async def _process_utterance(user_id: int, pcm: bytes) -> None:
    if user_id not in consented_users or not conversation_running:
        return
    assert _transcription_sem is not None
    async with _transcription_sem:
        try:
            text = await _transcribe_pcm(user_id, pcm)
            if user_id not in consented_users or not conversation_running:
                return
            if len(text) < 2:
                return
            speaker = _display_name(user_id)
            conversation_history.append({"role": "user", "speaker": speaker, "text": text, "ts": time.time()})
            _emit("transcript", text, user_id=user_id, speaker=speaker)
            await _maybe_ai_response(user_id, speaker, text)
        except asyncio.CancelledError:
            return
        except Exception as exc:
            _emit("error", f"Falha ao transcrever {_display_name(user_id)}: {type(exc).__name__}: {exc}", user_id=user_id)


def _schedule_utterance(user_id: int, pcm: bytes) -> None:
    if user_id not in consented_users or not conversation_running:
        return
    task = asyncio.create_task(_process_utterance(user_id, pcm), name=f"voice-transcribe-{user_id}")
    _transcription_tasks[user_id].add(task)

    def done(_task: asyncio.Task) -> None:
        bucket = _transcription_tasks.get(user_id)
        if bucket is not None:
            bucket.discard(_task)
            if not bucket:
                _transcription_tasks.pop(user_id, None)

    task.add_done_callback(done)


def _flush_buffer(user_id: int) -> None:
    buffer = _audio_buffers.pop(user_id, None)
    started = _audio_started_at.pop(user_id, None)
    _audio_last_speech_at.pop(user_id, None)
    if not buffer or started is None:
        return
    duration = len(buffer) / PCM_BYTES_PER_SECOND
    if duration < MIN_UTTERANCE_SECONDS:
        return
    _schedule_utterance(user_id, bytes(buffer))


async def _audio_worker() -> None:
    global current_speaker
    while True:
        item: tuple[int, bytes | None, float, bool] | None = None
        try:
            item = await asyncio.wait_for(_audio_queue.get(), timeout=0.15)
        except asyncio.TimeoutError:
            pass

        now = time.monotonic()
        if item is not None:
            user_id, payload, packet_at, is_speech = item
            try:
                if conversation_running and user_id in consented_users:
                    if is_speech and payload:
                        if not _audio_buffers[user_id]:
                            _audio_started_at[user_id] = packet_at
                            _emit("speaker", f"{_display_name(user_id)} começou a falar", user_id=user_id, speaker=_display_name(user_id))
                        _audio_buffers[user_id].extend(payload)
                        _audio_last_speech_at[user_id] = packet_at
                        active_speakers[user_id] = packet_at
                        current_speaker = {"userId": str(user_id), "name": _display_name(user_id), "since": time.time()}
                        if len(_audio_buffers[user_id]) / PCM_BYTES_PER_SECOND >= MAX_UTTERANCE_SECONDS:
                            _flush_buffer(user_id)
                    elif _audio_buffers.get(user_id) and packet_at - _audio_last_speech_at.get(user_id, packet_at) >= SILENCE_END_SECONDS:
                        _flush_buffer(user_id)
            finally:
                try:
                    _audio_queue.task_done()
                except ValueError:
                    pass

        for uid in list(_audio_buffers):
            last = _audio_last_speech_at.get(uid, now)
            if now - last >= SILENCE_END_SECONDS:
                _flush_buffer(uid)

        for uid, last in list(active_speakers.items()):
            if now - last >= 0.9:
                active_speakers.pop(uid, None)
        if current_speaker is not None:
            try:
                uid = int(current_speaker.get("userId", 0))
                if uid not in active_speakers:
                    current_speaker = None
            except Exception:
                current_speaker = None


async def _housekeeping_worker() -> None:
    while True:
        await asyncio.sleep(1.5)
        if not conversation_channel_id:
            continue
        present = {int(getattr(member, "id", 0) or 0) for member in _active_channel_members()}
        for uid in list(consented_users):
            if uid not in present:
                name = consented_users.get(uid, {}).get("name") or _display_name(uid)
                consented_users.pop(uid, None)
                _clear_user_audio(uid, cancel_tasks=True)
                _emit("consent", f"Consentimento de {name} encerrado ao sair da call.", user_id=uid, speaker=str(name), consented=False)


async def _ensure_workers() -> None:
    global _audio_worker_task, _housekeeping_task, _transcription_sem, _ai_lock, _event_loop
    _event_loop = asyncio.get_running_loop()
    if _transcription_sem is None:
        _transcription_sem = asyncio.Semaphore(2)
    if _ai_lock is None:
        _ai_lock = asyncio.Lock()
    if _audio_worker_task is None or _audio_worker_task.done():
        _audio_worker_task = asyncio.create_task(_audio_worker(), name="greed-voice-audio-worker")
    if _housekeeping_task is None or _housekeeping_task.done():
        _housekeeping_task = asyncio.create_task(_housekeeping_worker(), name="greed-voice-housekeeping")


async def start_conversation(channel_id: str, mode: str = "interactive") -> dict[str, Any]:
    global conversation_running, conversation_mode, conversation_channel_id, _listener_sink
    if not NATIVE_VOICE_AVAILABLE:
        raise HTTPException(status_code=503, detail=f"Native voice receiver unavailable: {NATIVE_VOICE_ERROR or 'not installed'}")
    if not base.GROQ_API_KEY:
        raise HTTPException(status_code=503, detail="GROQ_API_KEY is required for live transcription")

    await _ensure_workers()
    vc, channel = await ensure_native_voice_channel(channel_id)
    conversation_channel_id = str(channel.id)
    conversation_mode = "transcribe" if mode == "transcribe" else "interactive"
    conversation_running = True

    try:
        if not getattr(vc, "is_listening", lambda: False)():
            basic = BasicSink(_on_pcm_packet, media_types=("audio",), codecs=("pcm",))
            decoded = PCMDecodeSink(basic)
            filtered = ConditionalFilter(decoded, _consent_predicate)
            _listener_sink = filtered
            vc.listen(filtered, after=_listener_after)
    except Exception as exc:
        conversation_running = False
        conversation_mode = "off"
        raise HTTPException(status_code=500, detail=f"Unable to start voice receiver: {type(exc).__name__}: {exc}") from exc

    _emit(
        "system",
        f"Modo {'interativo' if conversation_mode == 'interactive' else 'somente transcrição'} iniciado. O áudio só é processado após {CONSENT_COMMAND}.",
    )
    return conversation_status_payload()


async def stop_conversation(*, clear_consent: bool = False) -> dict[str, Any]:
    global conversation_running, conversation_mode, current_speaker, _listener_sink, conversation_thinking
    conversation_running = False
    conversation_mode = "off"
    conversation_thinking = False
    current_speaker = None
    vc = voice.active_voice_client
    try:
        if vc is not None and getattr(vc, "is_listening", lambda: False)():
            vc.stop_listening()
    except Exception as exc:
        _emit("error", f"Erro ao parar recepção: {type(exc).__name__}: {exc}")
    _listener_sink = None
    _clear_all_audio(cancel_tasks=True)
    if clear_consent:
        consented_users.clear()
    _emit("system", "Transcrição da call parada.")
    return conversation_status_payload()


def conversation_status_payload() -> dict[str, Any]:
    vc = voice.active_voice_client
    return {
        "available": NATIVE_VOICE_AVAILABLE and bool(base.GROQ_API_KEY),
        "nativeVoiceAvailable": NATIVE_VOICE_AVAILABLE,
        "nativeVoiceError": NATIVE_VOICE_ERROR,
        "groqConfigured": bool(base.GROQ_API_KEY),
        "transcriptionModel": TRANSCRIPTION_MODEL,
        "conversationModel": CONVERSATION_MODEL,
        "running": conversation_running,
        "mode": conversation_mode,
        "thinking": conversation_thinking,
        "channelId": conversation_channel_id,
        "listening": bool(vc and getattr(vc, "is_listening", lambda: False)()),
        "currentSpeaker": current_speaker,
        "participants": _participant_payload(),
        "consentedUserIds": [str(uid) for uid in consented_users],
        "consentCommand": CONSENT_COMMAND,
        "revokeCommand": REVOKE_COMMAND,
        "events": list(recent_events),
        "rawAudioStored": False,
    }


async def _handle_consent_message(message: Any) -> bool:
    content = (getattr(message, "content", None) or "").strip().casefold()
    consent_tokens = {CONSENT_COMMAND.casefold(), "!consent", "!consentir"}
    revoke_tokens = {REVOKE_COMMAND.casefold(), "!revoke", "!revogar"}
    if content not in consent_tokens | revoke_tokens:
        return False

    guild = getattr(message, "guild", None) or getattr(getattr(message, "channel", None), "guild", None)
    if guild is None or str(getattr(guild, "id", "")) != CONVERSATION_GUILD_ID:
        return False
    author = getattr(message, "author", None)
    if author is None or bool(getattr(author, "bot", False)):
        return True
    uid = int(author.id)
    name = str(getattr(author, "display_name", None) or getattr(author, "name", uid))

    if content in consent_tokens:
        if not voice.active_voice_client or not getattr(voice.active_voice_client, "is_connected", lambda: False)():
            await message.reply("A transcrição não está em uma call agora. Entre na call e tente novamente.", mention_author=False)
            return True
        if not _same_active_call(uid):
            await message.reply("Entre na mesma call do Greed antes de autorizar a transcrição.", mention_author=False)
            return True
        consented_users[uid] = {"userId": str(uid), "name": name, "consentedAt": time.time()}
        _emit("consent", f"{name} autorizou a transcrição desta sessão.", user_id=uid, speaker=name, consented=True)
        await message.reply(
            f"✅ Consentimento ativado para esta sessão. Enquanto você estiver nesta call, sua voz pode ser transcrita e usada pela IA. Digite `{REVOKE_COMMAND}` para revogar a qualquer momento.",
            mention_author=False,
        )
        return True

    consented_users.pop(uid, None)
    _clear_user_audio(uid, cancel_tasks=True)
    _emit("consent", f"{name} revogou o consentimento.", user_id=uid, speaker=name, consented=False)
    await message.reply("✅ Consentimento revogado. Sua voz não será mais processada nesta sessão.", mention_author=False)
    return True


@client.event
async def on_message(message) -> None:
    handled = False
    try:
        handled = await _handle_consent_message(message)
    except Exception as exc:
        print(f"[VoiceConversation] consent handler failed: {type(exc).__name__}: {exc}", flush=True)
    if handled:
        return
    await _original_on_message(message)


@app.get("/api/conversation/status", dependencies=[Depends(base.require_api_token)])
async def conversation_status() -> dict[str, Any]:
    return conversation_status_payload()


@app.post("/api/conversation/action", dependencies=[Depends(base.require_api_token)])
async def conversation_action(body: dict[str, Any]) -> dict[str, Any]:
    global conversation_mode
    action = str(body.get("action") or "").strip().casefold()
    channel_id = str(body.get("channelId") or conversation_channel_id or "").strip()

    if action == "start":
        if not channel_id.isdigit():
            raise HTTPException(status_code=400, detail="channelId is required")
        return {"ok": True, "conversation": await start_conversation(channel_id, str(body.get("mode") or "interactive"))}

    if action == "mode":
        requested = str(body.get("mode") or "interactive").casefold()
        conversation_mode = "transcribe" if requested == "transcribe" else "interactive"
        if not conversation_running:
            conversation_mode = "off"
        else:
            _emit("system", f"Modo alterado para {'interativo' if conversation_mode == 'interactive' else 'somente transcrição'}.")
        return {"ok": True, "conversation": conversation_status_payload()}

    if action == "stop":
        return {"ok": True, "conversation": await stop_conversation(clear_consent=False)}

    if action == "leave":
        state = await stop_conversation(clear_consent=True)
        return {"ok": True, "conversation": state}

    if action == "clear-events":
        recent_events.clear()
        return {"ok": True, "conversation": conversation_status_payload()}

    raise HTTPException(status_code=400, detail="unknown conversation action")
