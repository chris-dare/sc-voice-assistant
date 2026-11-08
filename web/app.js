// Zigi voice POC client: chat with voice notes and read-aloud, plus a hands-free voice mode.
const $ = (id) => document.getElementById(id);
const state = { token: null, messages: new Map(), voice: null, audio: null, recorder: null, config: {} };

// ---------------------------------------------------------------- session
async function signIn() {
  let userId = null;
  try { userId = localStorage.getItem("zigi_user_id"); } catch {}
  const res = await fetch("/v1/session", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(userId ? { user_id: userId } : {}),
  });
  if (!res.ok) throw new Error(`sign-in failed (${res.status})`);
  const data = await res.json();
  try { localStorage.setItem("zigi_user_id", data.user_id); } catch {}
  state.token = data.token;
  state.config = await (await fetch("/v1/config")).json();
}

function api(path, opts = {}) {
  return fetch(path, { ...opts, headers: { ...(opts.headers || {}), authorization: `Bearer ${state.token}` } });
}

function toast(text, ms = 4000) {
  const el = $("toast");
  el.textContent = text;
  el.hidden = false;
  clearTimeout(toast.t);
  toast.t = setTimeout(() => (el.hidden = true), ms);
}

function setStatus(text) { $("status-line").textContent = text; }

// ---------------------------------------------------------------- messages
const DEBUG_LINES = [/^zigi 2 preprod$/i, /^PRODUCTION$/i];

function addMessage(m, { speech } = {}) {
  if (!m || state.messages.has(m.id)) return;
  const text = (m.content && m.content.text) || "";
  state.messages.set(m.id, m);
  const li = document.createElement("li");
  li.className = `msg ${m.author_type === "business" ? "bot" : "me"}`;
  if (DEBUG_LINES.some((r) => r.test(text.trim()))) li.classList.add("debug");
  li.dataset.id = m.id;
  const bubble = document.createElement("div");
  bubble.className = "bubble";
  bubble.innerHTML = linkify(text || describe(m.content));
  li.appendChild(bubble);
  if (m.metadata && m.metadata.input === "voice") li.classList.add("via-voice");
  if (m.author_type === "business") {
    const actions = (m.content.actions || []).filter((a) => a.type === "reply" || a.type === "postback");
    if (actions.length) {
      const chips = document.createElement("div");
      chips.className = "chips";
      for (const a of actions) {
        const b = document.createElement("button");
        b.type = "button";
        b.textContent = a.text;
        b.addEventListener("click", () => sendText(a.text));
        chips.appendChild(b);
      }
      li.appendChild(chips);
    }
    if (text.trim() && !li.classList.contains("debug")) {
      const speak = document.createElement("button");
      speak.type = "button";
      speak.className = "speak";
      speak.setAttribute("aria-label", "Listen to this reply");
      speak.title = "Listen";
      speak.innerHTML = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M3 10v4h4l5 5V5L7 10H3Zm13.5 2A4.5 4.5 0 0 0 14 8v8a4.5 4.5 0 0 0 2.5-4ZM14 3.2v2.1a7 7 0 0 1 0 13.4v2.1a9 9 0 0 0 0-17.6Z"/></svg>';
      speak.addEventListener("click", () => readAloud(m.id, speak));
      li.appendChild(speak);
    }
  }
  const list = $("messages");
  list.appendChild(li);
  list.scrollTop = list.scrollHeight;
}

function describe(content) {
  if (!content) return "";
  if (content.type === "carousel" || content.type === "list") return (content.items || []).map((i) => i.title).join(" · ");
  if (content.type === "image") return "[image]";
  if (content.type === "file") return "[file]";
  return `[${content.type}]`;
}

function escapeHtml(s) {
  return s.replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
}

function linkify(text) {
  // Text is escaped first; only markdown links and bare https URLs become anchors.
  let html = escapeHtml(text);
  html = html.replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');
  html = html.replace(/(^|[\s(])(https?:\/\/[^\s<)]+[^\s<).,;:!?])/g, '$1<a href="$2" target="_blank" rel="noopener">$2</a>');
  html = html.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  return html.replace(/\n/g, "<br>");
}

async function loadHistory() {
  const res = await api("/v1/conversations/me/messages");
  if (!res.ok) throw new Error(`history failed (${res.status})`);
  const data = await res.json();
  data.messages.forEach((m) => addMessage(m));
}

function openStream() {
  const es = new EventSource(`/v1/conversations/me/stream?token=${encodeURIComponent(state.token)}`);
  es.addEventListener("bot_message", (e) => {
    addMessage(JSON.parse(e.data));
    $("messages").classList.remove("waiting");
  });
  es.onerror = () => setStatus("Reconnecting…");
  es.onopen = () => setStatus("Online");
}

async function sendText(text, input = "typed") {
  text = text.trim();
  if (!text) return;
  $("messages").classList.add("waiting");
  const res = await api("/v1/messages", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ text, input }),
  });
  if (!res.ok) {
    $("messages").classList.remove("waiting");
    toast("Message not sent. Please try again.");
    return;
  }
  addMessage((await res.json()).message);
}

// ---------------------------------------------------------------- read-aloud (Flow 3)
async function readAloud(id, btn) {
  if (state.audio) {
    state.audio.pause();
    const wasSame = state.audio.dataset.id === id;
    document.querySelectorAll(".speak.playing").forEach((b) => b.classList.remove("playing"));
    state.audio = null;
    if (wasSame) return;
  }
  const audio = new Audio(`/v1/messages/${encodeURIComponent(id)}/audio?token=${encodeURIComponent(state.token)}`);
  audio.dataset.id = id;
  state.audio = audio;
  btn.classList.add("playing");
  audio.onended = audio.onerror = () => {
    btn.classList.remove("playing");
    if (audio.error) toast("Audio is unavailable for this reply.");
    if (state.audio === audio) state.audio = null;
  };
  try { await audio.play(); } catch { btn.classList.remove("playing"); toast("Tap again to play audio."); }
}

// ---------------------------------------------------------------- voice notes (Flow 2)
async function toggleVoiceNote() {
  if (state.recorder) { state.recorder.stop(); return; }
  let stream;
  try {
    stream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true } });
  } catch { toast("Microphone access is needed for voice notes."); return; }
  const mime = ["audio/webm;codecs=opus", "audio/mp4", "audio/webm"].find((t) => window.MediaRecorder && MediaRecorder.isTypeSupported(t)) || "";
  const rec = new MediaRecorder(stream, mime ? { mimeType: mime } : undefined);
  const chunks = [];
  const started = Date.now();
  rec.ondataavailable = (e) => e.data.size && chunks.push(e.data);
  const timer = setInterval(() => {
    const s = Math.floor((Date.now() - started) / 1000);
    $("rec-time").textContent = `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
    if (s >= 120) rec.stop();
  }, 250);
  rec.onstop = async () => {
    clearInterval(timer);
    stream.getTracks().forEach((t) => t.stop());
    state.recorder = null;
    $("recording").hidden = true;
    $("input").hidden = false;
    $("note-btn").classList.remove("recording-on");
    const blob = new Blob(chunks, { type: rec.mimeType || "audio/webm" });
    if (blob.size < 2000) { toast("That note was too short."); return; }
    $("input").placeholder = "Transcribing…";
    const form = new FormData();
    form.append("file", blob, `note.${blob.type.includes("mp4") ? "m4a" : "webm"}`);
    const res = await api("/v1/voice-notes", { method: "POST", body: form });
    $("input").placeholder = "Type a message";
    if (!res.ok) { toast("Couldn't transcribe that note. Please try again or type."); return; }
    const { transcript } = await res.json();
    if (!transcript) { toast("I didn't catch anything in that note."); return; }
    $("input").value = transcript;
    $("input").dataset.input = "voice_note";
    $("note-hint").hidden = false;
    autoGrow();
    $("input").focus();
  };
  state.recorder = rec;
  rec.start(250);
  $("recording").hidden = false;
  $("input").hidden = true;
  $("note-btn").classList.add("recording-on");
}

// ---------------------------------------------------------------- voice mode (Flow 1)
class VoiceClient {
  constructor() {
    this.ws = null;
    this.state = "idle";
    this.muted = false;
    this.speaking = false; // user speaking, per local VAD
    this.bargeIn = false;
    this.noise = 0.004;
    this.above = 0;
    this.below = 0;
    this.preroll = [];
    this.sampleRate = 24000;
  }

  async start() {
    this.micStream = await navigator.mediaDevices.getUserMedia({
      audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true, channelCount: 1 },
    });
    this.micCtx = new AudioContext({ sampleRate: 16000 });
    await this.micCtx.audioWorklet.addModule("/static/mic-worklet.js");
    this.mic = new AudioWorkletNode(this.micCtx, "mic-processor");
    this.micCtx.createMediaStreamSource(this.micStream).connect(this.mic);
    this.mic.port.onmessage = (e) => this.onFrame(e.data);

    this.outCtx = new AudioContext({ sampleRate: state.config.tts_sample_rate || 24000 });
    await this.outCtx.audioWorklet.addModule("/static/player-worklet.js");
    this.player = new AudioWorkletNode(this.outCtx, "player-processor", { outputChannelCount: [1] });
    this.player.connect(this.outCtx.destination);
    this.player.port.onmessage = (e) => {
      if (e.data.type === "level") setOrbLevel(e.data.rms * 6);
      if (e.data.type === "drained") { this.playing = false; setVoiceState(this.state); }
      if (e.data.type === "playing") { this.playing = true; setVoiceState("speaking"); }
    };

    const proto = location.protocol === "https:" ? "wss" : "ws";
    this.ws = new WebSocket(`${proto}://${location.host}/v1/voice/session?token=${encodeURIComponent(state.token)}`);
    this.ws.binaryType = "arraybuffer";
    this.ws.onmessage = (e) => (typeof e.data === "string" ? this.onEvent(JSON.parse(e.data)) : this.onAudio(e.data));
    this.ws.onclose = (e) => {
      if (this.ws) { toast(e.code === 4401 ? "Session expired. Reload the page." : "Voice session ended."); stopVoice(); }
    };
    await new Promise((resolve, reject) => {
      this.ws.onopen = resolve;
      setTimeout(() => reject(new Error("voice connection timed out")), 8000);
    });
  }

  stop() {
    const ws = this.ws;
    this.ws = null;
    try { ws && ws.close(); } catch {}
    try { this.micStream.getTracks().forEach((t) => t.stop()); } catch {}
    try { this.micCtx.close(); } catch {}
    try { this.outCtx.close(); } catch {}
  }

  send(obj) { if (this.ws && this.ws.readyState === 1) this.ws.send(JSON.stringify(obj)); }

  // Energy VAD with an adaptive noise floor. Drives speech hints, barge-in and mic gating.
  onFrame({ pcm, rms }) {
    if (this.muted || !this.ws || this.ws.readyState !== 1) return;
    const botTalking = this.state === "speaking" || this.playing;
    const thr = Math.max(this.noise * 3.2, 0.012);
    const bargeThr = Math.max(this.noise * 6, 0.04);
    if (!this.speaking) this.noise = this.noise * 0.97 + Math.min(rms, 0.05) * 0.03;

    if (botTalking && !this.bargeIn) {
      // Mic is gated while the assistant talks, so its own voice never reaches STT.
      this.preroll.push(pcm);
      if (this.preroll.length > 15) this.preroll.shift();
      this.above = rms > bargeThr ? this.above + 1 : 0;
      if (this.above >= 8) {
        this.bargeIn = true;
        this.player.port.postMessage({ type: "clear" });
        this.send({ type: "interrupt" });
        this.beginSpeech();
        for (const p of this.preroll) this.ws.send(p);
        this.preroll = [];
      }
      if (!this.speaking) setOrbLevel(0);
      return;
    }

    this.ws.send(pcm);
    setOrbLevel(this.state === "listening" ? rms * 12 : 0);
    if (!this.speaking) {
      this.above = rms > thr ? this.above + 1 : 0;
      if (this.above >= 3) this.beginSpeech();
    } else {
      this.below = rms < thr * 0.7 ? this.below + 1 : 0;
      if (this.below >= 25) {
        this.speaking = false;
        this.bargeIn = false;
        this.below = 0;
        this.send({ type: "speech_end", hangover_ms: 500 });
      }
    }
  }

  beginSpeech() {
    this.speaking = true;
    this.above = 0;
    this.below = 0;
    this.send({ type: "speech_start" });
  }

  onAudio(buf) {
    const pcm = new Int16Array(buf);
    const f = new Float32Array(pcm.length);
    for (let i = 0; i < pcm.length; i++) f[i] = pcm[i] / 32768;
    this.player.port.postMessage({ type: "push", samples: f }, [f.buffer]);
  }

  onEvent(ev) {
    switch (ev.type) {
      case "ready":
        setStatus(ev.stt_mode === "realtime" ? "Voice · live captions" : "Voice · batch STT");
        break;
      case "state":
        this.state = ev.state;
        setVoiceState(this.playing ? "speaking" : ev.state);
        break;
      case "transcript_partial":
        $("caption-user").textContent = ev.text;
        break;
      case "transcript_final":
        $("caption-user").textContent = ev.text;
        $("caption-bot").textContent = "";
        break;
      case "user_message":
        addMessage(ev.message);
        break;
      case "bot_message":
        addMessage(ev.message);
        if (ev.speech) $("caption-bot").textContent = ev.speech;
        break;
      case "audio_start":
        this.outCtx.resume();
        break;
      case "audio_stop":
        this.player.port.postMessage({ type: "clear" });
        break;
      case "metrics":
        showMetrics(ev);
        break;
      case "notice":
        toast(ev.text);
        break;
      case "error":
        toast(ev.text || "Something went wrong.");
        break;
      case "session_end":
        toast(ev.reason === "idle" ? "Voice chat paused after inactivity." : "Voice session ended.");
        stopVoice();
        break;
    }
  }
}

const STATE_LABELS = { idle: "Tap to start talking", listening: "Listening…", thinking: "Thinking…", speaking: "Speaking — talk to interrupt" };

function setVoiceState(s) {
  $("orb").dataset.state = s;
  $("voice-state").textContent = STATE_LABELS[s] || s;
}

function setOrbLevel(level) {
  $("orb").style.setProperty("--level", Math.min(1, Math.max(0, level)).toFixed(3));
}

function showMetrics(m) {
  const fmt = (v) => (v == null ? "–" : v >= 1000 ? `${(v / 1000).toFixed(1)} s` : `${v} ms`);
  const rows = [
    ["Speech → text", m.stt_ms],
    ["Bot reply", m.bot_ms],
    ["TTS first byte", m.tts_first_byte_ms],
    ["Gateway total (excl. bot)", m.gateway_ms],
    ["End of speech → audio", m.total_ms],
  ];
  $("metrics").innerHTML = rows.map(([k, v]) => `<div><dt>${k}</dt><dd>${fmt(v)}</dd></div>`).join("");
}

async function startVoice() {
  const btn = $("voice-btn");
  btn.disabled = true;
  try {
    state.voice = new VoiceClient();
    await state.voice.start();
    btn.textContent = "End voice chat";
    btn.setAttribute("aria-pressed", "true");
    $("mute-btn").disabled = false;
  } catch (err) {
    console.error(err);
    toast(err.name === "NotAllowedError" ? "Microphone access is needed for voice mode." : "Couldn't start voice chat.");
    stopVoice();
  } finally {
    btn.disabled = false;
  }
}

function stopVoice() {
  if (state.voice) state.voice.stop();
  state.voice = null;
  setVoiceState("idle");
  setOrbLevel(0);
  $("voice-btn").textContent = "Start voice chat";
  $("voice-btn").setAttribute("aria-pressed", "false");
  $("mute-btn").disabled = true;
  $("mute-btn").setAttribute("aria-pressed", "false");
  setStatus("Online");
}

// ---------------------------------------------------------------- UI wiring
function showTab(name) {
  for (const t of ["chat", "voice"]) {
    $(`tab-${t}`).setAttribute("aria-selected", String(t === name));
    $(`view-${t}`).hidden = t !== name;
  }
  if (name === "chat") $("messages").scrollTop = $("messages").scrollHeight;
}

function autoGrow() {
  const el = $("input");
  el.style.height = "auto";
  el.style.height = `${Math.min(el.scrollHeight, 120)}px`;
}

function wire() {
  $("tab-chat").addEventListener("click", () => showTab("chat"));
  $("tab-voice").addEventListener("click", () => showTab("voice"));
  $("composer").addEventListener("submit", (e) => {
    e.preventDefault();
    const input = $("input");
    const kind = input.dataset.input || "typed";
    sendText(input.value, kind);
    input.value = "";
    delete input.dataset.input;
    $("note-hint").hidden = true;
    autoGrow();
  });
  $("input").addEventListener("input", autoGrow);
  $("input").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); $("composer").requestSubmit(); }
  });
  $("note-btn").addEventListener("click", toggleVoiceNote);
  $("recording").addEventListener("click", toggleVoiceNote);
  $("voice-btn").addEventListener("click", () => (state.voice ? stopVoice() : startVoice()));
  $("mute-btn").addEventListener("click", () => {
    if (!state.voice) return;
    state.voice.muted = !state.voice.muted;
    $("mute-btn").setAttribute("aria-pressed", String(state.voice.muted));
    $("voice-state").textContent = state.voice.muted ? "Microphone muted" : STATE_LABELS[state.voice.state];
  });
}

(async function main() {
  wire();
  try {
    await signIn();
    await loadHistory();
    openStream();
    setStatus(state.config.mock ? "Online · mock providers" : "Online");
  } catch (err) {
    console.error(err);
    setStatus("Offline");
    toast("Can't reach the assistant right now.");
  }
})();
