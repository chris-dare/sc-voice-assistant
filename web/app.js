// Zigi voice POC client: chat with voice notes and read-aloud, plus a hands-free voice mode.
import { Orb } from "./orb.js";
import { initAnalytics, identify, track } from "./analytics.js";

const $ = (id) => document.getElementById(id);
let orb = null;
const state = { voiceMode: "relay", token: null, messages: new Map(), voice: null, audio: null, recorder: null, config: {}, pending: [], previews: new Map(), camera: null };

// ---------------------------------------------------------------- session
// Asks for the access code in the page itself: window.prompt is blocked in some in-app browsers,
// which locked people out (bursts of 403s in the logs).
function askCode(wrong) {
  return new Promise((resolve) => {
    const dlg = $("code-dialog");
    $("code-error").hidden = !wrong;
    $("code-input").value = "";
    $("code-form").onsubmit = (e) => {
      e.preventDefault();
      const v = $("code-input").value.trim();
      if (!v) return;
      dlg.close();
      resolve(v);
    };
    dlg.showModal();
    $("code-input").focus();
  });
}

async function signIn() {
  state.config = await (await fetch("/v1/config")).json();
  initAnalytics(state.config);
  let userId = null;
  let code = "";
  try { userId = localStorage.getItem("zigi_user_id"); code = localStorage.getItem("zigi_access_code") || ""; } catch {}
  let wrong = false;
  for (let attempt = 0; attempt < 5; attempt++) {
    if (state.config.access_code_required && !code) code = await askCode(wrong);
    const body = { ...(userId ? { user_id: userId } : {}), ...(code ? { access_code: code } : {}) };
    const res = await fetch("/v1/session", { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(body) });
    if (res.status === 403) {
      track("signin_failed", { reason: "bad_access_code", attempt });
      try { localStorage.removeItem("zigi_access_code"); } catch {}
      code = "";
      wrong = true;
      continue;
    }
    if (!res.ok) { track("signin_failed", { reason: `http_${res.status}`, attempt }); throw new Error(`sign-in failed (${res.status})`); }
    const data = await res.json();
    try { localStorage.setItem("zigi_user_id", data.user_id); if (code) localStorage.setItem("zigi_access_code", code); } catch {}
    state.token = data.token;
    identify(data.user_id);
    track("signin_succeeded", { attempts: attempt + 1, returning: !!userId });
    return;
  }
  throw new Error("access code rejected");
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
  const ctype = m.content && m.content.type;
  if ((ctype === "image" || ctype === "file") && m.content.mediaUrl) {
    li.appendChild(attachmentView(m.content));
  }
  if (text || !(ctype === "image" || ctype === "file")) {
    const bubble = document.createElement("div");
    bubble.className = "bubble";
    bubble.innerHTML = linkify(text || describe(m.content));
    li.appendChild(bubble);
  }
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

function mediaSrc(url) {
  return state.previews.get(url) || `/v1/media?url=${encodeURIComponent(url)}&token=${encodeURIComponent(state.token)}`;
}

function humanSize(bytes) {
  if (!bytes && bytes !== 0) return "";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1048576) return `${Math.round(bytes / 1024)} KB`;
  return `${(bytes / 1048576).toFixed(1)} MB`;
}

function attachmentView(content) {
  const src = mediaSrc(content.mediaUrl);
  const remote = `/v1/media?url=${encodeURIComponent(content.mediaUrl)}&token=${encodeURIComponent(state.token)}`;
  const a = document.createElement("a");
  a.href = remote;
  a.target = "_blank";
  a.rel = "noopener";
  if (content.type === "image") {
    a.className = "att-img";
    const img = document.createElement("img");
    img.src = src;
    img.alt = content.altText || "Image";
    img.loading = "lazy";
    img.onerror = () => { a.classList.add("broken"); img.remove(); a.textContent = content.altText || "Image unavailable"; };
    a.appendChild(img);
  } else {
    a.className = "att-file";
    a.innerHTML = '<span class="file-ico" aria-hidden="true"><svg viewBox="0 0 24 24"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8l-6-6Zm4 18H6V4h7v5h5v11Z"/></svg></span>';
    const meta = document.createElement("span");
    meta.className = "file-meta";
    const name = document.createElement("strong");
    name.textContent = content.altText || "File";
    const size = document.createElement("small");
    size.textContent = [humanSize(content.mediaSize), (content.mediaType || "").split("/").pop().toUpperCase()].filter(Boolean).join(" · ");
    meta.append(name, size);
    a.appendChild(meta);
  }
  return a;
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
    const m = JSON.parse(e.data);
    addMessage(m);
    if (!state.voice && voiceViewVisible() && m.content && m.content.text && !DEBUG_LINES.some((r) => r.test(m.content.text.trim()))) {
      setCaption("bot", m.content.text, null, { append: true });
    }
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
    track("message_failed", { input, status: res.status });
    return;
  }
  addMessage((await res.json()).message);
  track("message_sent", { input, view: voiceViewVisible() ? "voice" : "chat", in_call: !!state.voice, chars: text.length });
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
  const tapped = performance.now();
  audio.addEventListener("playing", () => track("read_aloud_played", { start_ms: Math.round(performance.now() - tapped) }), { once: true });
  audio.onended = audio.onerror = () => {
    btn.classList.remove("playing");
    if (audio.error) { toast("Audio is unavailable for this reply."); track("read_aloud_failed", { code: audio.error.code }); }
    if (state.audio === audio) state.audio = null;
  };
  try { await audio.play(); } catch { btn.classList.remove("playing"); toast("Tap again to play audio."); }
}

// ---------------------------------------------------------------- attachments
const DOC_TYPES = new Set([
  "application/pdf", "text/plain", "text/csv", "application/msword",
  "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
  "application/vnd.ms-excel", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
  "application/vnd.ms-powerpoint", "application/vnd.openxmlformats-officedocument.presentationml.presentation",
]);
const MAX_PENDING = 10;
const maxBytes = () => state.config.attachment_max_bytes || 4000000;

function isImage(f) { return f.type.startsWith("image/") && f.type !== "image/svg+xml"; }

// Phone photos are large: resize to 2048 px on the long side as JPEG, so uploads fit the 4 MB limit and stay fast on mobile data.
async function shrinkImage(file) {
  if (!isImage(file) || file.type === "image/gif" || !window.createImageBitmap) return file;
  let bmp;
  try { bmp = await createImageBitmap(file, { imageOrientation: "from-image" }); } catch { return file; }
  const scale = Math.min(1, 2048 / Math.max(bmp.width, bmp.height));
  if (scale === 1 && file.size <= 1500000) { bmp.close && bmp.close(); return file; }
  const canvas = document.createElement("canvas");
  canvas.width = Math.round(bmp.width * scale);
  canvas.height = Math.round(bmp.height * scale);
  canvas.getContext("2d").drawImage(bmp, 0, 0, canvas.width, canvas.height);
  bmp.close && bmp.close();
  const blob = await new Promise((r) => canvas.toBlob(r, "image/jpeg", 0.85));
  if (!blob) return file;
  const name = (file.name || "photo").replace(/\.[^.]+$/, "") + ".jpg";
  return new File([blob], name, { type: "image/jpeg" });
}

async function addFiles(fileList) {
  for (const raw of Array.from(fileList || [])) {
    if (state.pending.length >= MAX_PENDING) { toast(`You can attach up to ${MAX_PENDING} items at a time.`); break; }
    if (!isImage(raw) && !DOC_TYPES.has(raw.type)) { toast(`${raw.name || "That file"} isn't a supported type.`); continue; }
    const file = await shrinkImage(raw);
    if (file.size > maxBytes()) { toast(`${file.name} is larger than ${Math.round(maxBytes() / 1000000)} MB.`); continue; }
    state.pending.push({ id: crypto.randomUUID(), file, preview: isImage(file) ? URL.createObjectURL(file) : null });
    track("attachment_added", { kind: isImage(file) ? "image" : "file", source: addFiles.source || "unknown", size_kb: Math.round(file.size / 1024), resized: file !== raw });
  }
  renderTray();
}

function removePending(id) {
  const i = state.pending.findIndex((p) => p.id === id);
  if (i >= 0) {
    const [p] = state.pending.splice(i, 1);
    if (p.preview) URL.revokeObjectURL(p.preview);
  }
  renderTray();
}

function renderTray() {
  const tray = $("attach-tray");
  tray.innerHTML = "";
  for (const p of state.pending) {
    const item = document.createElement("div");
    item.className = p.preview ? "tray-item tray-img" : "tray-item tray-file";
    if (p.preview) {
      const img = document.createElement("img");
      img.src = p.preview;
      img.alt = p.file.name;
      item.appendChild(img);
    } else {
      const name = document.createElement("span");
      name.textContent = p.file.name;
      const size = document.createElement("small");
      size.textContent = humanSize(p.file.size);
      item.append(name, size);
    }
    const x = document.createElement("button");
    x.type = "button";
    x.className = "tray-remove";
    x.setAttribute("aria-label", `Remove ${p.file.name}`);
    x.textContent = "×";
    x.addEventListener("click", () => removePending(p.id));
    item.appendChild(x);
    tray.appendChild(item);
  }
  tray.hidden = state.pending.length === 0;
  updateComposerButtons();
}

function updateComposerButtons() {
  const hasContent = $("input").value.trim().length > 0 || state.pending.length > 0;
  const inCall = !!state.voice;
  $("send-btn").hidden = !hasContent;
  $("voice-mode-btn").hidden = hasContent || inCall;
  $("mute-btn").hidden = !inCall;
  $("end-btn").hidden = !inCall;
  $("note-btn").hidden = inCall; // the mic is busy with the call
  $("input").placeholder = inCall ? "Type while you talk" : "Ask Zigi";
}

function voiceViewVisible() { return !$("view-voice").hidden; }

// The bot often sends several messages per turn: captions keep all of them until the user speaks or types again.
function setCaption(which, text, thumb, { append = false } = {}) {
  const el = $(which === "user" ? "caption-user" : "caption-bot");
  if (append && text) {
    const para = document.createElement("p");
    para.textContent = text;
    el.appendChild(para);
    return;
  }
  el.textContent = "";
  if (thumb) {
    const img = document.createElement("img");
    img.src = thumb;
    img.alt = "";
    img.className = "cap-thumb";
    el.appendChild(img);
  }
  if (text) el.appendChild(document.createTextNode(text));
  if (which === "bot") $("captions").scrollTop = 0;
}

async function sendAttachments(items) {
  for (const p of items) {
    const form = new FormData();
    form.append("file", p.file, p.file.name);
    $("messages").classList.add("waiting");
    try {
      const res = await api("/v1/attachments", { method: "POST", body: form });
      if (!res.ok) {
        const detail = (await res.json().catch(() => ({}))).detail;
        toast(`${p.file.name} wasn't sent${detail ? `: ${detail}` : "."}`);
        track("attachment_failed", { kind: p.preview ? "image" : "file", status: res.status });
        continue;
      }
      const { message } = await res.json();
      track("attachment_sent", { kind: p.preview ? "image" : "file", size_kb: Math.round(p.file.size / 1024), view: voiceViewVisible() ? "voice" : "chat", in_call: !!state.voice });
      if (p.preview) state.previews.set(message.content.mediaUrl, p.preview); // show the local copy at once
      addMessage(message);
      if (voiceViewVisible()) {
        setCaption("user", p.preview ? "Photo sent" : `Sent ${p.file.name}`, p.preview);
        setCaption("bot", "");
        if (state.voice) setVoiceState("thinking");
      }
    } catch {
      toast(`${p.file.name} wasn't sent. Check your connection.`);
    }
  }
}

// Attach menu: Camera / Photos / Files.
function toggleMenu(open) {
  const menu = $("attach-menu");
  const show = open ?? menu.hidden;
  menu.hidden = !show;
  $("plus-btn").setAttribute("aria-expanded", String(show));
  if (show) $("menu-camera").focus();
}

const isTouch = () => window.matchMedia("(pointer: coarse)").matches;
// iOS always shows its own Photo Library / Take Photo / Choose Files sheet for any input that accepts images,
// and web pages cannot open the photo library directly. So on iOS the + button opens that sheet in one tap.
const isIOS = () => /iPad|iPhone|iPod/.test(navigator.userAgent) || (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);
const DOC_ACCEPT = ".pdf,.txt,.csv,.doc,.docx,.xls,.xlsx,.ppt,.pptx," + [...DOC_TYPES].join(",");

async function openCamera() {
  // Phones: the file input's capture attribute opens the native camera app.
  if (isTouch() || !navigator.mediaDevices?.getUserMedia) { $("pick-camera").click(); return; }
  // Laptops: capture in the page from the webcam.
  const dlg = $("camera-dialog");
  state.camera = { facing: "user", stream: null };
  try {
    await startCameraStream();
    dlg.showModal();
  } catch {
    toast("Camera unavailable. Choose a photo instead.");
    $("pick-photos").click();
  }
}

async function startCameraStream() {
  stopCameraStream();
  const stream = await navigator.mediaDevices.getUserMedia({ video: { facingMode: state.camera.facing, width: { ideal: 1920 } }, audio: false });
  state.camera.stream = stream;
  $("camera-video").srcObject = stream;
}

function stopCameraStream() {
  if (state.camera && state.camera.stream) state.camera.stream.getTracks().forEach((t) => t.stop());
  if (state.camera) state.camera.stream = null;
}

function closeCamera() {
  stopCameraStream();
  const dlg = $("camera-dialog");
  if (dlg.open) dlg.close();
}

async function shoot() {
  const video = $("camera-video");
  if (!video.videoWidth) return;
  const canvas = document.createElement("canvas");
  canvas.width = video.videoWidth;
  canvas.height = video.videoHeight;
  canvas.getContext("2d").drawImage(video, 0, 0);
  const blob = await new Promise((r) => canvas.toBlob(r, "image/jpeg", 0.88));
  closeCamera();
  addFiles.source = "webcam";
  if (blob) await addFiles([new File([blob], `photo-${Date.now()}.jpg`, { type: "image/jpeg" })]);
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
    if (!res.ok) { toast("Couldn't transcribe that note. Please try again or type."); track("voice_note_failed", { status: res.status }); return; }
    const { transcript, stt_ms } = await res.json();
    track("voice_note_transcribed", { duration_s: Math.round((Date.now() - started) / 1000), stt_ms, empty: !transcript });
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
      if (e.data.type === "playing") {
        this.playing = true;
        this.playbackStartedAt = performance.now();
        setVoiceState("speaking");
        // Time from the end of the user's turn to the reply audibly playing on this device.
        if (this.awaitingReply && this.turnEndedAt) {
          track("voice_reply_heard", { heard_ms: Math.round(performance.now() - this.turnEndedAt), input: this.turnInput, mode: this.mode });
          this.awaitingReply = false;
        }
      }
    };

    this.active = true;
    this.retryDelay = 500;
    this.startedAt = performance.now();
    this.turns = 0;
    this.interruptions = 0;
    await this.openSocket();
  }

  // Hosted on serverless functions, the socket closes at the function's time limit: reconnect quietly.
  openSocket() {
    const proto = location.protocol === "https:" ? "wss" : "ws";
    this.mode = state.voiceMode;
    const ws = new WebSocket(`${proto}://${location.host}/v1/voice/session?token=${encodeURIComponent(state.token)}&mode=${this.mode}`);
    ws.binaryType = "arraybuffer";
    this.ws = ws;
    ws.onmessage = (e) => (typeof e.data === "string" ? this.onEvent(JSON.parse(e.data)) : this.onAudio(e.data));
    ws.onclose = (e) => {
      if (!this.active || this.ws !== ws) return;
      if (e.code === 4401) { toast("Session expired. Reload the page."); stopVoice("expired"); return; }
      track("voice_reconnect", { code: e.code, after_s: Math.round((performance.now() - this.startedAt) / 1000) });
      setStatus("Voice · reconnecting…");
      this.speaking = false;
      setTimeout(() => this.active && this.openSocket().catch(() => {}), this.retryDelay);
      this.retryDelay = Math.min(this.retryDelay * 2, 15000);
    };
    return new Promise((resolve, reject) => {
      ws.onopen = () => { this.retryDelay = 500; resolve(); };
      setTimeout(() => reject(new Error("voice connection timed out")), 8000);
    });
  }

  stop() {
    this.active = false;
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
        this.interruptions = (this.interruptions || 0) + 1;
        // Barge-ins within ~0.5 s of playback starting are often the assistant hearing itself (echo).
        const into = this.playbackStartedAt ? Math.round(performance.now() - this.playbackStartedAt) : null;
        track("voice_barge_in", { ms_into_playback: into, likely_echo: into !== null && into < 500 });
        this.player.port.postMessage({ type: "clear" });
        this.send({ type: "interrupt" });
        this.beginSpeech();
        for (const p of this.preroll) this.ws.send(p);
        this.preroll = [];
      }
      if (!this.speaking) setOrbLevel(0);
      return;
    }

    // Noise gate: until the local VAD hears speech, STT gets silence of the same length, so background
    // noise and voices are never transcribed. The last ~240 ms of real audio is replayed when speech starts.
    const gate = state.config.noise_gate !== false;
    if (this.speaking || !gate) {
      this.ws.send(pcm);
    } else {
      this.gateBuf = this.gateBuf || [];
      this.gateBuf.push(pcm);
      if (this.gateBuf.length > 12) this.gateBuf.shift();
      this.ws.send(new ArrayBuffer(pcm.byteLength));
    }
    setOrbLevel(this.state === "listening" ? rms * 12 : 0);
    if (!this.speaking) {
      this.above = rms > thr ? this.above + 1 : 0;
      if (this.above >= 3) {
        this.beginSpeech();
        if (gate && this.gateBuf) { for (const p of this.gateBuf) this.ws.send(p); this.gateBuf = []; }
      }
    } else {
      this.below = rms < thr * 0.7 ? this.below + 1 : 0;
      if (this.below >= 25) {
        this.speaking = false;
        this.bargeIn = false;
        this.below = 0;
        this.send({ type: "speech_end", hangover_ms: 500 });
        this.markTurnEnd("voice", 500);
      }
    }
  }

  markTurnEnd(input, hangoverMs = 0) {
    this.turnEndedAt = performance.now() - hangoverMs;
    this.turnInput = input;
    this.awaitingReply = true;
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
        this.mode = ev.mode || "relay";
        setStatus(`Voice · ${this.mode === "agent" ? "Agentic" : "Chatbot relay"}${ev.stt_mode === "realtime" ? "" : " · batch STT"}`);
        break;
      case "state":
        this.state = ev.state;
        setVoiceState(this.playing ? "speaking" : ev.state);
        break;
      case "transcript_partial":
        setCaption("user", ev.text);
        break;
      case "transcript_final":
        setCaption("user", ev.text);
        setCaption("bot", "");
        break;
      case "user_message":
        addMessage(ev.message);
        break;
      case "bot_message":
        addMessage(ev.message);
        if (ev.speech) setCaption("bot", (ev.message.content && ev.message.content.text) || ev.speech, null, { append: true });
        break;
      case "agent_text":
        setCaption("bot", ev.text, null, { append: true });
        addAgentText(ev.turn_id, ev.text);
        break;
      case "audio_start":
        this.outCtx.resume();
        if (ev.hold) this.heldReply = true;
        break;
      case "audio_stop":
        this.player.port.postMessage({ type: "clear" });
        break;
      case "metrics":
        this.turns += 1;
        track("voice_turn", { ...ev, type: undefined, held: !!this.heldReply });
        this.heldReply = false;
        showMetrics(ev);
        $("timings").hidden = false;
        break;
      case "notice":
        toast(ev.text);
        break;
      case "error":
        toast(ev.text || "Something went wrong.");
        break;
      case "session_end":
        if (ev.reason === "idle") { toast("Voice chat paused after inactivity."); stopVoice("idle"); }
        // max_duration: the socket closes next and onclose reconnects.
        break;
    }
  }
}

const STATE_LABELS = { idle: "Tap the orb to start talking", listening: "Listening…", thinking: "Thinking…", speaking: "Speaking — talk to interrupt" };

function setVoiceState(s) {
  if (orb) orb.setState(s);
  $("voice-state").textContent = STATE_LABELS[s] || s;
  $("orb-btn").setAttribute("aria-label", s === "idle" ? "Start voice chat" : `Voice chat: ${STATE_LABELS[s] || s}`);
}

function setOrbLevel(level) {
  if (orb) orb.setLevel(level);
}

function showMetrics(m) {
  const fmt = (v) => (v == null ? "–" : typeof v === "string" ? v : v >= 1000 ? `${(v / 1000).toFixed(1)} s` : `${v} ms`);
  const rows = m.mode === "agent"
    ? [
        ["Route", { direct: "Agent answered", zigi: "Asked Zigi", fallback: "Fell back to chatbot relay", push: "Zigi update" }[m.route] || m.route],
        ["Speech → text", m.stt_ms],
        ["Agent first word", m.llm_ms],
        ["Zigi", m.bot_ms],
        ["First audio (incl. hold line)", m.first_audio_ms],
        ["End of speech → answer audio", m.total_ms],
      ]
    : [
        ["Speech → text", m.stt_ms],
        ["Bot reply", m.bot_ms],
        ["TTS first byte", m.tts_first_byte_ms],
        ["Gateway total (excl. bot)", m.gateway_ms],
        ["End of speech → audio", m.total_ms],
      ];
  $("metrics").innerHTML = rows.map(([k, v]) => `<div><dt>${escapeHtml(k)}</dt><dd>${escapeHtml(fmt(v))}</dd></div>`).join("");
}

// Agent mode: what the agent said, one bubble per turn, marked so it can't be mistaken for Zigi's own text.
function addAgentText(turnId, text) {
  let li = document.querySelector(`.msg.agent[data-turn="${CSS.escape(turnId)}"]`);
  if (!li) {
    li = document.createElement("li");
    li.className = "msg bot agent";
    li.dataset.turn = turnId;
    li.innerHTML = '<div class="bubble"></div><span class="tag">Agentic · spoken</span>';
    $("messages").appendChild(li);
  }
  const bubble = li.querySelector(".bubble");
  bubble.textContent = bubble.textContent ? `${bubble.textContent} ${text}` : text;
  $("messages").scrollTop = $("messages").scrollHeight;
}

function setVoiceMode(mode, { user = false } = {}) {
  if (!(state.config.voice_modes || ["relay"]).includes(mode)) mode = "relay";
  const changed = mode !== state.voiceMode;
  state.voiceMode = mode;
  document.querySelectorAll("#mode-switch [data-mode]").forEach((b) => b.setAttribute("aria-checked", String(b.dataset.mode === mode)));
  if (!user || !changed) return;
  try { localStorage.setItem("voiceMode", mode); } catch {}
  track("voice_mode_changed", { to: mode, in_call: !!state.voice });
  // Mid-call: reconnect the socket in the new mode; the mic and audio stay open.
  const v = state.voice;
  if (v && v.ws) {
    const old = v.ws;
    old.onclose = null;
    try { old.close(); } catch {}
    v.ws = null;
    v.openSocket().catch(() => toast("Couldn't switch mode. Try again."));
  }
  toast(mode === "agent" ? "Agentic: an agent answers small talk and asks Zigi for the rest." : "Chatbot relay: everything you say goes to Zigi.", 3000);
}

async function startVoice() {
  if (state.voice || startVoice.busy) return;
  startVoice.busy = true;
  showTab("voice");
  $("voice-state").textContent = "Connecting…";
  try {
    state.voice = new VoiceClient();
    updateComposerButtons();
    await state.voice.start();
    setVoiceState("listening");
    track("voice_call_started", { mode: state.voiceMode });
  } catch (err) {
    console.error(err);
    toast(err.name === "NotAllowedError" ? "Microphone access is needed for voice mode." : "Couldn't start voice chat.");
    track("voice_call_start_failed", { error: err.name || "Error", mic_denied: err.name === "NotAllowedError" });
    stopVoice("start_failed");
  } finally {
    startVoice.busy = false;
  }
}

function stopVoice(reason = "user") {
  const v = state.voice;
  if (v && v.startedAt) {
    track("voice_call_ended", {
      reason: typeof reason === "string" ? reason : "user",
      duration_s: Math.round((performance.now() - v.startedAt) / 1000),
      turns: v.turns || 0,
      mode: v.mode || state.voiceMode,
      interruptions: v.interruptions || 0,
    });
  }
  if (state.voice) state.voice.stop();
  state.voice = null;
  setVoiceState("idle");
  setOrbLevel(0);
  $("mute-btn").setAttribute("aria-pressed", "false");
  updateComposerButtons();
  setStatus("Online");
}

// ---------------------------------------------------------------- UI wiring
function showTab(name) {
  if ($(`view-${name}`).hidden) track("view_switched", { to: name });
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
  document.querySelectorAll("#mode-switch [data-mode]").forEach((b) => b.addEventListener("click", () => setVoiceMode(b.dataset.mode, { user: true })));
  $("tab-chat").addEventListener("click", () => showTab("chat"));
  $("tab-voice").addEventListener("click", () => showTab("voice"));
  $("composer").addEventListener("submit", async (e) => {
    e.preventDefault();
    const input = $("input");
    const kind = input.dataset.input || "typed";
    const text = input.value;
    const items = state.pending.splice(0);
    input.value = "";
    delete input.dataset.input;
    $("note-hint").hidden = true;
    renderTray();
    autoGrow();
    if (items.length) await sendAttachments(items);
    if (!text.trim()) return;
    if (state.voice && state.voice.ws && state.voice.ws.readyState === 1) {
      state.voice.send({ type: "text", text: text.trim() }); // spoken reply, same turn tracking as speech
      state.voice.markTurnEnd("typed");
      track("message_sent", { input: "typed", view: "voice", in_call: true, chars: text.trim().length });
    } else {
      await sendText(text, kind);
      if (voiceViewVisible()) { setCaption("user", text.trim()); setCaption("bot", ""); }
    }
  });
  $("input").addEventListener("input", () => { autoGrow(); updateComposerButtons(); });
  $("input").addEventListener("paste", (e) => {
    const files = Array.from(e.clipboardData?.files || []);
    if (files.length) { e.preventDefault(); addFiles.source = "paste"; addFiles(files); }
  });
  const chat = $("view-chat");
  chat.addEventListener("dragover", (e) => { if (e.dataTransfer?.types?.includes("Files")) { e.preventDefault(); chat.classList.add("dropping"); } });
  chat.addEventListener("dragleave", (e) => { if (e.target === chat) chat.classList.remove("dropping"); });
  chat.addEventListener("drop", (e) => { e.preventDefault(); chat.classList.remove("dropping"); addFiles.source = "drop"; addFiles(e.dataTransfer.files); });
  // On phones "Files" asks for documents only, so iOS and Android open their file browser directly
  // instead of a photo-or-file chooser. Laptops keep images in the file dialog too.
  if (isTouch()) $("pick-files").accept = DOC_ACCEPT;
  $("plus-btn").addEventListener("click", (e) => {
    e.stopPropagation();
    if (isIOS()) { $("pick-ios").click(); return; }
    toggleMenu();
  });
  document.addEventListener("click", (e) => { if (!$("attach-menu").hidden && !$("attach-menu").contains(e.target)) toggleMenu(false); });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape" && !$("attach-menu").hidden) { toggleMenu(false); $("plus-btn").focus(); } });
  $("menu-camera").addEventListener("click", () => { toggleMenu(false); openCamera(); });
  $("menu-photos").addEventListener("click", () => { toggleMenu(false); $("pick-photos").click(); });
  $("menu-files").addEventListener("click", () => { toggleMenu(false); $("pick-files").click(); });
  for (const id of ["pick-camera", "pick-photos", "pick-files", "pick-ios"]) {
    $(id).addEventListener("change", (e) => { addFiles.source = id.replace("pick-", ""); addFiles(e.target.files); e.target.value = ""; });
  }
  $("camera-cancel").addEventListener("click", closeCamera);
  $("camera-dialog").addEventListener("close", stopCameraStream);
  $("camera-shoot").addEventListener("click", shoot);
  $("camera-flip").addEventListener("click", async () => {
    state.camera.facing = state.camera.facing === "user" ? "environment" : "user";
    try { await startCameraStream(); } catch { toast("No other camera found."); }
  });
  $("voice-mode-btn").addEventListener("click", () => startVoice());
  $("orb-btn").addEventListener("click", () => { if (!state.voice) startVoice(); });
  $("end-btn").addEventListener("click", () => stopVoice("user"));
  $("input").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); $("composer").requestSubmit(); }
  });
  $("note-btn").addEventListener("click", toggleVoiceNote);
  $("recording").addEventListener("click", toggleVoiceNote);
  $("mute-btn").addEventListener("click", () => {
    if (!state.voice) return;
    const v = state.voice;
    v.muted = !v.muted;
    // Muting mid-sentence must end the utterance, or the gateway keeps waiting for the user and holds the reply.
    if (v.muted && v.speaking) {
      v.speaking = false;
      v.bargeIn = false;
      v.send({ type: "speech_end", hangover_ms: 0 });
    }
    $("mute-btn").setAttribute("aria-pressed", String(v.muted));
    track(v.muted ? "voice_muted" : "voice_unmuted", {});
    $("mute-btn").setAttribute("aria-label", v.muted ? "Unmute microphone" : "Mute microphone");
    $("voice-state").textContent = v.muted ? "Microphone muted" : STATE_LABELS[v.state];
  });
}

// Size the app to the visible screen. iOS overlays the keyboard instead of resizing the page,
// so follow the visual viewport and keep the composer above the keyboard.
function fitViewport() {
  const vv = window.visualViewport;
  const h = vv ? vv.height : window.innerHeight;
  document.documentElement.style.setProperty("--app-h", `${Math.round(h)}px`);
  if (vv && vv.offsetTop > 0) window.scrollTo(0, 0);
}

window.addEventListener("error", (e) => track("client_error", { kind: "error", name: (e.error && e.error.name) || "Error" }));
window.addEventListener("unhandledrejection", (e) => track("client_error", { kind: "promise", name: (e.reason && e.reason.name) || "Error" }));

(async function main() {
  orb = new Orb($("orb"));
  fitViewport();
  window.visualViewport?.addEventListener("resize", fitViewport);
  window.visualViewport?.addEventListener("scroll", fitViewport);
  window.addEventListener("orientationchange", () => setTimeout(fitViewport, 300));
  $("input").addEventListener("focus", () => setTimeout(() => { $("messages").scrollTop = $("messages").scrollHeight; }, 300));
  wire();
  try {
    await signIn();
    const modes = state.config.voice_modes || ["relay"];
    $("mode-switch").hidden = modes.length < 2;
    let saved = null;
    try { saved = localStorage.getItem("voiceMode"); } catch {}
    setVoiceMode(saved || state.config.voice_mode_default || "relay");
    await loadHistory();
    openStream();
    setStatus(state.config.mock ? "Online · mock providers" : "Online");
  } catch (err) {
    console.error(err);
    setStatus("Offline");
    toast("Can't reach the assistant right now.");
  }
})();
