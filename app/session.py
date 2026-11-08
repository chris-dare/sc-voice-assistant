"""Session Manager + Turn Coordinator for one voice-mode WebSocket (Flow 1 in the design).

Client -> gateway
  binary frames: 16 kHz mono PCM16 (20 ms frames recommended)
  {"type": "speech_start"} / {"type": "speech_end", "hangover_ms": 400}   client VAD hints
  {"type": "interrupt"}                                                   barge-in
  {"type": "text", "text": "..."}                                         typed input in voice mode
  {"type": "ping"}
Gateway -> client
  {"type": "ready", "conversation_id", "stt_mode", "sample_rate"}
  {"type": "state", "state": "listening" | "thinking" | "speaking"}
  {"type": "transcript_partial" | "transcript_final", "text"}
  {"type": "bot_message", "message": {...}, "speech": "..."}
  {"type": "audio_start", "message_id", "sample_rate"} binary PCM16 chunks {"type": "audio_end", "message_id"}
  {"type": "audio_stop"}                                                  playback cancelled
  {"type": "metrics", ...per-turn timings in ms...}
  {"type": "notice" | "error", "code", "text"}
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from fastapi import WebSocket, WebSocketDisconnect

from . import telemetry
from .bus import channel_for
from .delivery import SeenSet
from .services import Services, UserContext
from .speech import render, split_sentences
from .stt import RealtimeSTT, STTError, pcm16_to_wav
from .sunco import Message, SunCoError
from .tts import TTSError

log = logging.getLogger(__name__)

_HAS_WORD = re.compile(r"[A-Za-z0-9]")
_PREROLL_BYTES = 16000  # 0.5 s of 16 kHz PCM16 kept before speech_start (batch mode)


@dataclass
class Turn:
    id: str
    text: str
    t_speech_end: float | None
    t_final: float
    t_posted: float | None = None
    t_bot_any: float | None = None  # first bot message of any kind
    t_bot_first: float | None = None  # first bot message that is spoken
    t_tts_first: float | None = None
    reported: bool = False


@dataclass
class SpeakItem:
    message_id: str
    text: str
    turn: Turn | None = None
    is_hold: bool = False


@dataclass
class _State:
    name: str = "listening"
    user_speaking: bool = False
    last_partial: float = 0.0
    last_activity: float = field(default_factory=time.monotonic)


class VoiceSession:
    def __init__(self, ws: WebSocket, svc: Services, ctx: UserContext) -> None:
        self.ws = ws
        self.svc = svc
        self.cfg = svc.settings
        self.ctx = ctx
        self.state = _State()
        self.rt: RealtimeSTT | None = None
        self.stt_mode = self.cfg.stt_mode
        self.queue: asyncio.Queue[SpeakItem] = asyncio.Queue()
        self.speaking: asyncio.Task[None] | None = None
        self.turn: Turn | None = None
        self.hold_task: asyncio.Task[None] | None = None
        self.seen = SeenSet()
        self.batch_buf = bytearray()
        self.speech_end_at: float | None = None
        self._send_lock = asyncio.Lock()
        self._closed = False

    # ------------------------------------------------------------------ plumbing
    async def send(self, data: dict[str, Any]) -> None:
        if self._closed:
            return
        async with self._send_lock:
            with contextlib.suppress(Exception):
                await self.ws.send_text(json.dumps(data))

    async def send_audio(self, chunk: bytes) -> None:
        if self._closed:
            return
        async with self._send_lock:
            await self.ws.send_bytes(chunk)

    async def set_state(self, name: str) -> None:
        if self.state.name != name:
            self.state.name = name
            await self.send({"type": "state", "state": name})

    # ------------------------------------------------------------------ lifecycle
    async def run(self) -> None:
        if self.stt_mode == "realtime":
            await self._open_realtime()
        await self.send(
            {
                "type": "ready",
                "conversation_id": self.ctx.conversation_id,
                "stt_mode": self.stt_mode,
                "sample_rate": self.cfg.tts_sample_rate,
            }
        )
        await self.send({"type": "state", "state": "listening"})
        async with self.svc.bus.subscribe(channel_for(self.ctx.conversation_id)) as sub, self.svc.watcher.watch(self.ctx.conversation_id):
            tasks = [
                asyncio.create_task(self._recv_loop(), name="recv"),
                asyncio.create_task(self._bot_loop(sub), name="bot"),
                asyncio.create_task(self._speaker_loop(), name="speaker"),
                asyncio.create_task(self._watchdog(), name="watchdog"),
            ]
            if self.rt is not None:
                tasks.append(asyncio.create_task(self._stt_loop(), name="stt"))
            try:
                done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for t in done:
                    if t.exception() and not isinstance(t.exception(), WebSocketDisconnect):
                        log.exception("voice session task %s failed", t.get_name(), exc_info=t.exception())
            finally:
                self._closed = True
                for t in tasks:
                    t.cancel()
                if self.speaking:
                    self.speaking.cancel()
                if self.hold_task:
                    self.hold_task.cancel()
                if self.rt:
                    await self.rt.close()

    async def _open_realtime(self) -> None:
        try:
            self.rt = await self.svc.stt.open_realtime()
            self.stt_mode = "realtime"
        except STTError as exc:
            log.warning("falling back to batch STT: %s", exc)
            self.rt = None
            self.stt_mode = "batch"
            await self.send(
                {"type": "notice", "code": "stt_batch_fallback", "text": "Live captions are unavailable; using batch speech recognition."}
            )

    # ------------------------------------------------------------------ inbound
    async def _recv_loop(self) -> None:
        while True:
            msg = await self.ws.receive()
            if msg["type"] == "websocket.disconnect":
                raise WebSocketDisconnect(msg.get("code", 1000))
            if (pcm := msg.get("bytes")) is not None:
                await self._on_audio(pcm)
            elif (text := msg.get("text")) is not None:
                try:
                    data = json.loads(text)
                except ValueError:
                    continue
                await self._on_control(data)

    async def _on_audio(self, pcm: bytes) -> None:
        if self.rt is not None:
            try:
                await self.rt.send_pcm(pcm)
            except Exception as exc:
                log.warning("STT send failed: %s", exc)
                await self._reconnect_stt()
        else:
            self.batch_buf.extend(pcm)
            if not self.state.user_speaking and len(self.batch_buf) > _PREROLL_BYTES:
                del self.batch_buf[: len(self.batch_buf) - _PREROLL_BYTES]

    async def _on_control(self, data: dict[str, Any]) -> None:
        kind = data.get("type")
        now = time.monotonic()
        if kind == "speech_start":
            self.state.user_speaking = True
            self.state.last_activity = now
        elif kind == "speech_end":
            self.state.user_speaking = False
            self.state.last_activity = now
            hangover = float(data.get("hangover_ms") or 0) / 1000
            self.speech_end_at = now - hangover
            if self.rt is None and len(self.batch_buf) > _PREROLL_BYTES + 6400:
                audio, self.batch_buf = bytes(self.batch_buf), bytearray()
                asyncio.create_task(self._batch_transcribe(audio))
        elif kind == "interrupt":
            await self._interrupt()
        elif kind == "text":
            text = str(data.get("text") or "").strip()
            if text:
                self.speech_end_at = None
                await self._user_text(text[:2000], source="typed")
        elif kind == "ping":
            await self.send({"type": "pong"})

    async def _batch_transcribe(self, pcm: bytes) -> None:
        try:
            text = await self.svc.stt.transcribe(pcm16_to_wav(pcm), "utterance.wav", "audio/wav")
        except STTError as exc:
            await self.send({"type": "error", "code": "stt_failed", "text": str(exc)})
            return
        if _HAS_WORD.search(text):
            await self._user_text(text, source="voice")

    async def _stt_loop(self) -> None:
        while True:
            assert self.rt is not None
            async for ev in self.rt.events():
                if ev.kind == "partial" and ev.text.strip():
                    self.state.last_partial = time.monotonic()
                    self.state.last_activity = self.state.last_partial
                    await self.send({"type": "transcript_partial", "text": ev.text})
                elif ev.kind == "final":
                    if _HAS_WORD.search(ev.text):
                        await self._user_text(ev.text.strip(), source="voice")
                elif ev.kind == "error":
                    log.warning("STT error: %s", ev.text)
                    break
            if self._closed:
                return
            await self._reconnect_stt()
            if self.rt is None:
                return

    async def _reconnect_stt(self) -> None:
        if self.rt is not None:
            with contextlib.suppress(Exception):
                await self.rt.close()
        self.rt = None
        await self._open_realtime()

    # ------------------------------------------------------------------ turns
    async def _user_text(self, text: str, source: str) -> None:
        now = time.monotonic()
        speech_end = self.speech_end_at if source == "voice" else None
        self.speech_end_at = None
        turn = Turn(id=uuid.uuid4().hex[:12], text=text, t_speech_end=speech_end, t_final=now)
        self.turn = turn
        await self._interrupt(silent=True)
        await self.send({"type": "transcript_final", "text": text, "source": source, "turn_id": turn.id})
        await self.set_state("thinking")
        try:
            posted = await self.svc.provider.post_text(
                self.ctx.conversation_id,
                self.ctx.sunco_user_id,
                text,
                {"channel": "voice", "source": "sc-voice-gateway", "input": source},
            )
        except SunCoError as exc:
            log.warning("post to SunCo failed: %s", exc)
            await self.send({"type": "error", "code": "send_failed", "text": "I couldn't reach the assistant. Please try again."})
            await self.set_state("listening")
            return
        turn.t_posted = time.monotonic()
        self.seen.add(posted.id)
        await self.send({"type": "user_message", "message": posted.to_dict()})
        if self.hold_task:
            self.hold_task.cancel()
        self.hold_task = asyncio.create_task(self._hold(turn))

    async def _hold(self, turn: Turn) -> None:
        """Holding phrase while the bot is slow; timeout notice if it never answers."""
        if self.cfg.hold_prompt:
            await asyncio.sleep(self.cfg.hold_after_s)
            if self.turn is turn and turn.t_bot_any is None and not self.state.user_speaking:
                self.queue.put_nowait(SpeakItem("hold", self.cfg.hold_prompt, is_hold=True))
            await asyncio.sleep(max(0.0, self.cfg.bot_timeout_s - self.cfg.hold_after_s))
        else:
            await asyncio.sleep(self.cfg.bot_timeout_s)
        if self.turn is turn and turn.t_bot_any is None:
            await self.send({"type": "notice", "code": "bot_timeout", "text": "The assistant is taking longer than usual."})
            await self.set_state("listening")

    async def _bot_loop(self, sub: Any) -> None:
        async for event in sub:
            if event.get("kind") != "bot_message":
                continue
            msg = Message.from_dict(event["message"])
            if not self.seen.add(msg.id):
                continue
            turn = self.turn
            if turn and turn.t_bot_any is None:
                turn.t_bot_any = time.monotonic()
            speech = render(msg.content)
            spoken = self.svc.speakable(msg.text) and bool(speech)
            await self.send({"type": "bot_message", "message": msg.to_dict(), "speech": speech if spoken else ""})
            if spoken:
                if turn and turn.t_bot_first is None:
                    turn.t_bot_first = time.monotonic()
                self.queue.put_nowait(SpeakItem(msg.id, speech, turn))

    # ------------------------------------------------------------------ speaking
    async def _speaker_loop(self) -> None:
        while True:
            item = await self.queue.get()
            if item.is_hold and (self.turn is None or self.turn.t_bot_any is not None):
                continue  # the real answer arrived first
            # Never talk over the user: wait while they speak (client VAD or live partials).
            while self.state.user_speaking or time.monotonic() - self.state.last_partial < 0.8:  # noqa: ASYNC110
                await asyncio.sleep(0.1)
            self.speaking = asyncio.create_task(self._speak(item))
            # wait() rather than await: a barge-in cancels the speaking task, not this loop.
            await asyncio.wait({self.speaking})
            self.speaking = None
            if self.queue.empty():
                waiting = self.turn is not None and self.turn.t_bot_first is None and self.turn.t_bot_any is None
                await self.set_state("thinking" if waiting else "listening")

    async def _speak(self, item: SpeakItem) -> None:
        rate = self.cfg.tts_sample_rate
        fmt = f"pcm_{rate}"
        sentences = split_sentences(item.text)[:40]
        if not sentences:
            return
        await self.set_state("speaking")
        await self.send({"type": "audio_start", "message_id": item.message_id, "sample_rate": rate, "hold": item.is_hold})
        lookahead = 2
        queues: list[asyncio.Queue[bytes | Exception | None]] = []
        fetchers: list[asyncio.Task[None]] = []

        async def fetch(text: str, out: asyncio.Queue[bytes | Exception | None]) -> None:
            try:
                async for chunk in self.svc.tts.stream(text, self.cfg.tts_model_voice, fmt):
                    out.put_nowait(chunk)
            except TTSError as exc:
                out.put_nowait(exc)
            finally:
                out.put_nowait(None)

        def start(i: int) -> None:
            if i < len(sentences) and i == len(queues):
                q: asyncio.Queue[bytes | Exception | None] = asyncio.Queue()
                queues.append(q)
                fetchers.append(asyncio.create_task(fetch(sentences[i][: self.cfg.tts_max_chars], q)))

        try:
            for i in range(min(lookahead, len(sentences))):
                start(i)
            carry = b""
            for i in range(len(sentences)):
                start(i + lookahead - 1)
                while (chunk := await queues[i].get()) is not None:
                    if isinstance(chunk, Exception):
                        log.warning("TTS failed: %s", chunk)
                        await self.send({"type": "error", "code": "tts_failed", "text": "Audio is unavailable; the reply is on screen."})
                        return
                    data = carry + chunk
                    cut = len(data) - (len(data) % 2)  # keep whole PCM16 samples
                    carry = data[cut:]
                    if cut:
                        if item.turn and item.turn.t_tts_first is None and not item.is_hold:
                            item.turn.t_tts_first = time.monotonic()
                            await self._report(item.turn)
                        await self.send_audio(data[:cut])
            await self.send({"type": "audio_end", "message_id": item.message_id})
        finally:
            for f in fetchers:
                f.cancel()

    async def _interrupt(self, silent: bool = False) -> None:
        while not self.queue.empty():
            self.queue.get_nowait()
        if self.speaking and not self.speaking.done():
            self.speaking.cancel()
            await self.send({"type": "audio_stop"})
        if not silent:
            await self.set_state("listening")

    async def _report(self, turn: Turn) -> None:
        if turn.reported or turn.t_posted is None or turn.t_bot_first is None or turn.t_tts_first is None:
            return
        turn.reported = True

        def ms(a: float | None, b: float | None) -> int | None:
            return None if a is None or b is None else round((b - a) * 1000)

        start = turn.t_speech_end if turn.t_speech_end is not None else turn.t_final
        bot_ms = ms(turn.t_posted, turn.t_bot_first) or 0
        total = ms(start, turn.t_tts_first) or 0
        metrics = {
            "type": "metrics",
            "turn_id": turn.id,
            "stt_ms": ms(turn.t_speech_end, turn.t_final),
            "post_ms": ms(turn.t_final, turn.t_posted),
            "bot_ms": bot_ms,
            "tts_first_byte_ms": ms(turn.t_bot_first, turn.t_tts_first),
            "gateway_ms": total - bot_ms,
            "total_ms": total,
        }
        telemetry.turn_metrics(metrics)
        await self.send(metrics)

    async def _watchdog(self) -> None:
        started = time.monotonic()
        while True:
            await asyncio.sleep(5)
            now = time.monotonic()
            if now - started > self.cfg.session_max_s:
                await self.send({"type": "session_end", "reason": "max_duration"})
                return
            busy = self.state.name != "listening" or self.state.user_speaking
            if not busy and now - self.state.last_activity > self.cfg.idle_timeout_s:
                await self.send({"type": "session_end", "reason": "idle"})
                return
            if busy:
                self.state.last_activity = now
