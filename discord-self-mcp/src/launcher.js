import crypto from 'node:crypto';
import http from 'node:http';
import { spawn } from 'node:child_process';

const PUBLIC_PORT = Number(process.env.PORT || 10000);
const INTERNAL_MCP_PORT = Number(process.env.INTERNAL_MCP_PORT || 10002);
const BRIDGE_PORT = Number(process.env.BRIDGE_INTERNAL_PORT || 10001);
const API_TOKEN = process.env.API_TOKEN || '';
const LOGIN_SECRET = process.env.MCP_LOGIN_SECRET || API_TOKEN || '';
const DEFAULT_VOICE_CHANNEL_ID = process.env.DEFAULT_VOICE_CHANNEL_ID || '1530374141625106522';
const COOKIE_NAME = 'assistir_session';
const COOKIE_TTL = 60 * 60 * 12;

const child = spawn(process.execPath, ['src/index.js'], {
  cwd: process.cwd(),
  env: { ...process.env, PORT: String(INTERNAL_MCP_PORT) },
  stdio: 'inherit'
});

child.on('exit', (code, signal) => {
  console.error(`[Launcher] internal MCP exited code=${code} signal=${signal || ''}`);
});

function safeEqual(a, b) {
  const aa = Buffer.from(String(a ?? ''));
  const bb = Buffer.from(String(b ?? ''));
  return aa.length === bb.length && crypto.timingSafeEqual(aa, bb);
}

function hmac(value) {
  return crypto.createHmac('sha256', LOGIN_SECRET).update(value).digest('base64url');
}

function createSession() {
  const exp = Math.floor(Date.now() / 1000) + COOKIE_TTL;
  return `${exp}.${hmac(String(exp))}`;
}

function validSession(req) {
  if (!LOGIN_SECRET) return false;
  const cookies = Object.fromEntries(String(req.headers.cookie || '').split(';').map(v => v.trim()).filter(Boolean).map(v => {
    const i = v.indexOf('=');
    return i >= 0 ? [v.slice(0, i), decodeURIComponent(v.slice(i + 1))] : [v, ''];
  }));
  const value = cookies[COOKIE_NAME] || '';
  const [expText, sig] = value.split('.');
  const exp = Number(expText);
  if (!exp || exp < Math.floor(Date.now() / 1000) || !sig) return false;
  return safeEqual(sig, hmac(expText));
}

async function readBody(req, limit = 64 * 1024) {
  const chunks = [];
  let size = 0;
  for await (const chunk of req) {
    size += chunk.length;
    if (size > limit) throw new Error('request too large');
    chunks.push(chunk);
  }
  return Buffer.concat(chunks).toString('utf8');
}

async function bridge(path, options = {}) {
  const headers = { ...(options.headers || {}), authorization: `Bearer ${API_TOKEN}` };
  if (options.body && !headers['content-type']) headers['content-type'] = 'application/json';
  const response = await fetch(`http://127.0.0.1:${BRIDGE_PORT}${path}`, { ...options, headers });
  const text = await response.text();
  let data;
  try { data = text ? JSON.parse(text) : {}; } catch { data = { raw: text }; }
  if (!response.ok) throw new Error(data?.detail || data?.error || `HTTP ${response.status}`);
  return data;
}

function json(res, status, value) {
  const body = JSON.stringify(value);
  res.writeHead(status, { 'content-type': 'application/json; charset=utf-8', 'cache-control': 'no-store' });
  res.end(body);
}

function loginPage(error = '') {
  return `<!doctype html><html lang="pt-BR"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Greed Voice</title><style>body{margin:0;background:#0b1020;color:#edf2ff;font-family:Inter,Arial,sans-serif;min-height:100vh;display:grid;place-items:center}.card{width:min(420px,calc(100% - 32px));background:#121a2d;border:1px solid #26324d;border-radius:22px;padding:28px;box-shadow:0 22px 70px #0008}h1{margin:0 0 8px}.muted{color:#9caccc;line-height:1.5}input,button{box-sizing:border-box;width:100%;padding:13px 14px;border-radius:12px;font-size:16px}input{border:1px solid #33415f;background:#0b1020;color:white;margin:14px 0}button{border:0;background:#5865f2;color:white;font-weight:800;cursor:pointer}.err{color:#ff8e9a}</style></head><body><main class="card"><h1>🎙️ Greed Voice</h1><p class="muted">Painel privado de controle da call.</p>${error ? `<p class="err">${error}</p>` : ''}<form method="post" action="/assistir/login"><input type="password" name="access_key" placeholder="MCP_LOGIN_SECRET" autocomplete="current-password" required><button>Entrar</button></form></main></body></html>`;
}

function dashboardPage() {
  return `<!doctype html><html lang="pt-BR"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Greed Voice — Assistir</title><style>
*{box-sizing:border-box}body{margin:0;background:#080d19;color:#edf2ff;font-family:Inter,Arial,sans-serif}.wrap{max-width:1180px;margin:auto;padding:24px}.top{display:flex;gap:14px;align-items:center;justify-content:space-between;flex-wrap:wrap}.badge{padding:7px 11px;border-radius:999px;background:#18233b;color:#b9c7e6;font-size:13px}.grid{display:grid;grid-template-columns:1.2fr .8fr;gap:18px;margin-top:18px}@media(max-width:850px){.grid{grid-template-columns:1fr}}.card{background:#11192a;border:1px solid #22304b;border-radius:18px;padding:18px;box-shadow:0 15px 45px #0004}.controls{display:grid;grid-template-columns:1fr auto auto;gap:9px}.controls input{min-width:0}.btn,input,select{border-radius:11px;padding:11px 12px;font-size:14px}.btn{border:0;background:#5865f2;color:#fff;font-weight:750;cursor:pointer}.btn.secondary{background:#24314d}.btn.danger{background:#a83d4c}.btn.green{background:#258b61}input,select{background:#0a1020;border:1px solid #2d3c5c;color:#fff}.status{display:grid;grid-template-columns:repeat(4,1fr);gap:9px;margin-top:14px}@media(max-width:600px){.status{grid-template-columns:repeat(2,1fr)}}.stat{background:#0b1221;border-radius:12px;padding:12px}.stat b{display:block;font-size:18px;margin-top:4px}.small{color:#96a7c7;font-size:13px}.timeline{max-height:560px;overflow:auto;display:flex;flex-direction:column;gap:8px}.event{background:#0b1221;border-left:3px solid #39496c;padding:10px 12px;border-radius:8px}.event.response{border-left-color:#8c7cff}.event.transcript{border-left-color:#52c99a}.event.thinking{border-left-color:#e5b654}.event.error{border-left-color:#ef6673}.person{display:flex;align-items:center;justify-content:space-between;padding:9px 0;border-bottom:1px solid #1d2940}.ok{color:#61dba9}.no{color:#8290aa}.warn{background:#332a16;border:1px solid #645226;color:#f3d98c;border-radius:12px;padding:11px 13px;margin-top:12px}.speaker{font-size:22px;font-weight:800;margin:8px 0 2px}.row{display:flex;gap:8px;flex-wrap:wrap;margin-top:10px}code{color:#a9d5ff}</style></head><body><div class="wrap"><div class="top"><div><h1 style="margin:0">🎙️ Greed Voice</h1><div class="small">Painel ao vivo da call</div></div><div><span id="connectionBadge" class="badge">carregando…</span> <button class="btn secondary" onclick="logout()">Sair do painel</button></div></div>
<div class="grid"><section><div class="card"><h2>Controle</h2><div class="controls"><input id="channelId" value="${DEFAULT_VOICE_CHANNEL_ID}" placeholder="ID da call"><button class="btn green" onclick="join()">Entrar + conversar</button><button class="btn" onclick="joinTranscribe()">Só transcrever</button></div><div class="row"><button class="btn" onclick="setMode('interactive')">Modo interativo</button><button class="btn secondary" onclick="setMode('transcribe')">Só transcrição</button><button class="btn secondary" onclick="stopAudio()">Parar áudio</button><button class="btn danger" onclick="leave()">Sair da call</button></div><div class="status"><div class="stat"><span class="small">Call</span><b id="call">—</b></div><div class="stat"><span class="small">Modo</span><b id="mode">—</b></div><div class="stat"><span class="small">IA</span><b id="thinking">—</b></div><div class="stat"><span class="small">Falando</span><b id="speaking">—</b></div></div><div class="warn">⚠️ A recepção/transcrição contínua do áudio da call pela conta pessoal não está habilitada. O painel não salva áudio bruto. Os comandos de call e TTS continuam funcionais.</div></div>
<div class="card" style="margin-top:18px"><h2>🎙️ Quem está falando</h2><div id="speaker" class="speaker">Ninguém detectado</div><div class="small">A detecção de fala depende do módulo de recepção de voz.</div></div>
<div class="card" style="margin-top:18px"><h2>💬 Transcrição e respostas</h2><div id="timeline" class="timeline"><div class="event">Nenhuma transcrição recebida.</div></div></div></section>
<aside><div class="card"><h2>Participantes autorizados</h2><div class="small">Quando a transcrição estiver disponível, o consentimento deverá ser explícito por participante.</div><div id="participants" style="margin-top:10px"><div class="small">Sem dados de recepção de voz.</div></div></div><div class="card" style="margin-top:18px"><h2>Privacidade</h2><p class="small">🔒 Protegido pelo <code>MCP_LOGIN_SECRET</code>.</p><p class="small">🧹 Nenhum áudio bruto da call é armazenado por este painel.</p><p class="small">📣 Ao entrar, Greed anuncia automaticamente que a call pode ser ouvida/processada.</p></div></aside></div></div>
<script>
let last = {};
async function api(action,payload={}){const r=await fetch('/assistir/api/action',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({action,...payload})});const d=await r.json();if(!r.ok)throw new Error(d.error||'falha');return d}
async function refresh(){try{const r=await fetch('/assistir/api/status',{cache:'no-store'});if(r.status===401){location.reload();return}const d=await r.json();last=d;document.getElementById('connectionBadge').textContent=d.voice?.connected?'🟢 conectado':'⚪ desconectado';document.getElementById('call').textContent=d.voice?.channelName||'fora da call';document.getElementById('mode').textContent=d.mode||'controle';document.getElementById('thinking').textContent=d.thinking?'🧠 pensando':'ociosa';document.getElementById('speaking').textContent=d.voice?.playing?'🔊 sim':'não';}catch(e){document.getElementById('connectionBadge').textContent='🔴 erro'}}
async function join(){try{await api('join',{channelId:document.getElementById('channelId').value,mode:'interactive'});await refresh()}catch(e){alert(e.message)}}
async function joinTranscribe(){try{await api('join',{channelId:document.getElementById('channelId').value,mode:'transcribe'});await refresh()}catch(e){alert(e.message)}}
async function setMode(mode){try{await api('mode',{mode});await refresh()}catch(e){alert(e.message)}}
async function stopAudio(){try{await api('stop');await refresh()}catch(e){alert(e.message)}}
async function leave(){try{await api('leave');await refresh()}catch(e){alert(e.message)}}
async function logout(){await fetch('/assistir/logout',{method:'POST'});location.reload()}
setInterval(refresh,900);refresh();
</script></body></html>`;
}

function proxyToInternal(req, res) {
  const headers = { ...req.headers, host: req.headers.host || `127.0.0.1:${INTERNAL_MCP_PORT}` };
  const proxy = http.request({ hostname: '127.0.0.1', port: INTERNAL_MCP_PORT, path: req.url, method: req.method, headers }, upstream => {
    res.writeHead(upstream.statusCode || 502, upstream.headers);
    upstream.pipe(res);
  });
  proxy.on('error', err => {
    if (!res.headersSent) res.writeHead(502, { 'content-type': 'application/json' });
    res.end(JSON.stringify({ error: `internal service unavailable: ${err.message}` }));
  });
  req.pipe(proxy);
}

const server = http.createServer(async (req, res) => {
  try {
    const url = new URL(req.url || '/', `http://${req.headers.host || 'localhost'}`);
    if (!url.pathname.startsWith('/assistir')) return proxyToInternal(req, res);

    if (url.pathname === '/assistir/login' && req.method === 'POST') {
      const body = await readBody(req);
      const params = new URLSearchParams(body);
      if (!LOGIN_SECRET || !safeEqual(params.get('access_key') || '', LOGIN_SECRET)) {
        res.writeHead(401, { 'content-type': 'text/html; charset=utf-8' });
        return res.end(loginPage('Chave inválida.'));
      }
      res.writeHead(303, { location: '/assistir', 'set-cookie': `${COOKIE_NAME}=${encodeURIComponent(createSession())}; Path=/assistir; HttpOnly; Secure; SameSite=Strict; Max-Age=${COOKIE_TTL}` });
      return res.end();
    }

    if (url.pathname === '/assistir/logout' && req.method === 'POST') {
      res.writeHead(204, { 'set-cookie': `${COOKIE_NAME}=; Path=/assistir; HttpOnly; Secure; SameSite=Strict; Max-Age=0` });
      return res.end();
    }

    if (!validSession(req)) {
      if (url.pathname.startsWith('/assistir/api/')) return json(res, 401, { error: 'unauthorized' });
      res.writeHead(200, { 'content-type': 'text/html; charset=utf-8', 'cache-control': 'no-store' });
      return res.end(loginPage());
    }

    if (url.pathname === '/assistir' && req.method === 'GET') {
      res.writeHead(200, { 'content-type': 'text/html; charset=utf-8', 'cache-control': 'no-store', 'content-security-policy': "default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'" });
      return res.end(dashboardPage());
    }

    if (url.pathname === '/assistir/api/status' && req.method === 'GET') {
      const voiceStatus = await bridge('/api/status');
      let voice = { connected: false, playing: false };
      try {
        const target = DEFAULT_VOICE_CHANNEL_ID;
        const result = await bridge('/api/send', { method: 'POST', body: JSON.stringify({ to: `voice:${target}`, message: '/status' }) });
        voice = result.voice || voice;
      } catch {}
      return json(res, 200, { account: voiceStatus.user || null, voice, mode: 'controle', thinking: false, transcriptionAvailable: false, events: [], participants: [] });
    }

    if (url.pathname === '/assistir/api/action' && req.method === 'POST') {
      const body = JSON.parse(await readBody(req) || '{}');
      const action = String(body.action || '');
      const channelId = String(body.channelId || DEFAULT_VOICE_CHANNEL_ID).trim();
      if (action === 'join') {
        if (!/^\d{10,30}$/.test(channelId)) return json(res, 400, { error: 'ID da call inválido' });
        const result = await bridge('/api/send', { method: 'POST', body: JSON.stringify({ to: `voice:${channelId}`, message: '/join' }) });
        return json(res, 200, { ok: true, mode: body.mode || 'interactive', result });
      }
      if (action === 'leave') {
        const result = await bridge('/api/send', { method: 'POST', body: JSON.stringify({ to: 'voice:leave', message: 'sair' }) });
        return json(res, 200, { ok: true, result });
      }
      if (action === 'stop') {
        const result = await bridge('/api/send', { method: 'POST', body: JSON.stringify({ to: `voice:${channelId}`, message: '/stop' }) });
        return json(res, 200, { ok: true, result });
      }
      if (action === 'mode') return json(res, 200, { ok: true, mode: body.mode === 'transcribe' ? 'transcribe' : 'interactive', transcriptionAvailable: false });
      return json(res, 400, { error: 'ação desconhecida' });
    }

    return json(res, 404, { error: 'not found' });
  } catch (err) {
    return json(res, 500, { error: err.message || String(err) });
  }
});

server.listen(PUBLIC_PORT, '0.0.0.0', () => {
  console.log(`[Launcher] public service listening on 0.0.0.0:${PUBLIC_PORT}`);
  console.log(`[Launcher] protected dashboard: /assistir`);
  console.log(`[Launcher] internal MCP proxy: 127.0.0.1:${INTERNAL_MCP_PORT}`);
});

function shutdown() {
  try { child.kill('SIGTERM'); } catch {}
  try { server.close(); } catch {}
}
process.once('SIGTERM', shutdown);
process.once('SIGINT', shutdown);
