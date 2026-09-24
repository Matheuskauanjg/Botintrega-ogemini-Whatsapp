const originalFetch = globalThis.fetch.bind(globalThis);
const INTERNAL_AUDIO_PATH = '/api/audio';
const DEFAULT_MODEL = 'gpt-transcribe';

function normalizeMime(value) {
  const raw = String(value || '').toLowerCase();
  if (raw.includes('ogg')) return 'audio/ogg';
  if (raw.includes('webm')) return 'audio/webm';
  if (raw.includes('mpeg') || raw.includes('mp3')) return 'audio/mpeg';
  if (raw.includes('wav')) return 'audio/wav';
  if (raw.includes('mp4') || raw.includes('m4a')) return 'audio/mp4';
  return 'audio/ogg';
}

function extensionForMime(mime) {
  if (mime === 'audio/webm') return 'webm';
  if (mime === 'audio/mpeg') return 'mp3';
  if (mime === 'audio/wav') return 'wav';
  if (mime === 'audio/mp4') return 'm4a';
  return 'ogg';
}

function isInternalAudioRequest(input) {
  try {
    const url = new URL(typeof input === 'string' ? input : input?.url);
    return (url.hostname === '127.0.0.1' || url.hostname === 'localhost') && url.pathname === INTERNAL_AUDIO_PATH;
  } catch {
    return false;
  }
}

async function transcribeAudio(data) {
  const apiKey = String(process.env.OPENAI_API_KEY || '').trim();
  if (!apiKey) {
    return {
      transcript: null,
      transcriptionStatus: 'not_configured',
      transcriptionError: 'OPENAI_API_KEY is not configured on Render.'
    };
  }

  const audioBase64 = String(data?.audioBase64 || '');
  if (!audioBase64) {
    return {
      transcript: null,
      transcriptionStatus: 'empty_audio',
      transcriptionError: 'Downloaded WhatsApp audio did not include audioBase64.'
    };
  }

  let buffer;
  try {
    buffer = Buffer.from(audioBase64, 'base64');
  } catch {
    return {
      transcript: null,
      transcriptionStatus: 'invalid_audio',
      transcriptionError: 'Could not decode downloaded WhatsApp audio.'
    };
  }

  if (!buffer.length) {
    return {
      transcript: null,
      transcriptionStatus: 'empty_audio',
      transcriptionError: 'Downloaded WhatsApp audio is empty.'
    };
  }

  const model = String(process.env.OPENAI_TRANSCRIBE_MODEL || DEFAULT_MODEL).trim() || DEFAULT_MODEL;
  const mime = normalizeMime(data?.mimetype);
  const extension = extensionForMime(mime);
  const messageId = String(data?.messageId || 'audio').replace(/[^a-zA-Z0-9_-]/g, '').slice(0, 80) || 'audio';

  const form = new FormData();
  form.set('model', model);
  form.set('file', new Blob([buffer], { type: mime }), `whatsapp-${messageId}.${extension}`);

  const response = await originalFetch('https://api.openai.com/v1/audio/transcriptions', {
    method: 'POST',
    headers: {
      authorization: `Bearer ${apiKey}`
    },
    body: form
  });

  const raw = await response.text();
  let payload;
  try { payload = raw ? JSON.parse(raw) : {}; }
  catch { payload = { raw }; }

  if (!response.ok) {
    const detail = payload?.error?.message || payload?.message || `HTTP ${response.status}`;
    return {
      transcript: null,
      transcriptionStatus: 'error',
      transcriptionModel: model,
      transcriptionError: String(detail).slice(0, 500)
    };
  }

  return {
    transcript: typeof payload?.text === 'string' ? payload.text.trim() : '',
    transcriptionStatus: 'ok',
    transcriptionModel: model,
    detectedLanguages: Array.isArray(payload?.languages) ? payload.languages : undefined
  };
}

globalThis.fetch = async function patchedFetch(input, init) {
  const response = await originalFetch(input, init);
  if (!isInternalAudioRequest(input) || !response.ok) return response;

  try {
    const data = await response.clone().json();
    if (!data?.audioBase64) return response;

    const transcription = await transcribeAudio(data);
    const enriched = { ...data, ...transcription };

    const headers = new Headers(response.headers);
    headers.set('content-type', 'application/json; charset=utf-8');
    headers.delete('content-length');

    console.log(`[Audio] transcription status=${transcription.transcriptionStatus} model=${transcription.transcriptionModel || '(none)'} chars=${transcription.transcript?.length || 0}`);

    return new Response(JSON.stringify(enriched), {
      status: response.status,
      statusText: response.statusText,
      headers
    });
  } catch (error) {
    console.error('[Audio] transcription patch failed:', error?.message || error);
    return response;
  }
};

console.log('[Audio] WhatsApp transcription patch enabled.');
