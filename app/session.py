"""Session Manager + Turn Coordinator for one voice-mode WebSocket (Flow 1 in the design).

Two modes (design 5.8): "relay" posts every user turn to Zigi and speaks Zigi's replies;
"agent" puts an LLM in front that answers small talk itself and calls Zigi as a tool.

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
  {"type": "agent_text", "turn_id", "text"}                               agent mode: a sentence the agent says
  {"type": "metrics", ...per-turn timings in ms...}
  {"type": "notice" | "error", "code", "text"}
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

import logfire
from fastapi import WebSocket, WebSocketDisconnect

from zigi_agent.rules import ZigiState, spoken_digits, state_from_dict, state_to_dict

from . import telemetry
from .agent_client import AgentServiceError
from .bus import channel_for
from .delivery import SeenSet
from .services import Services, UserContext
from .speech import render, split_sentences
from .store import LimitExceeded
from .stt import RealtimeSTT, STTError, pcm16_to_wav
from .sunco import Message, SunCoError
from .tts import TTSError

log = logging.getLogger(__name__)

_HAS_WORD = re.compile(r"[A-Za-z0-9]")
_WORDS = re.compile(r"[a-z0-9']+")
# Transcripts made only of fillers or noise words are not a turn ("Uh, uh, uh").
_FILLER = re.compile(r"^(?:\s|[.,!?…-])*(?:(?:uh|um+|uhm|hmm+|mm+|ah+|er+|eh|oh|huh|hm)(?:\s|[.,!?…-])*)+$", re.I)
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
    # agent mode
    route: str = "relay"  # relay | direct | zigi | fallback | push
    relay: bool = True  # this turn's bot replies are spoken as they come (relay mode, or agent fallback)
    t_llm_first: float | None = None
    t_audio_first: float | None = None  # any audio, the hold line included
    zigi_ms: float = 0.0
    t_tool_start: float | None = None  # agent: first Zigi or web call started
    t_tool_end: float | None = None  # agent: last one finished
    audio_marks: list[tuple[float, float]] = field(default_factory=list)  # agent: (sentence received, its audio started)
    done: bool = False  # agent: the service finished the turn (route known)
    filler_spoken: bool = False
    spoken: list[str] = field(default_factory=list)
    note: str = ""  # agent: extra context for this turn (not the caller's words)
    tts_cache_hits: int = 0  # sentences spoken from the TTS cache
    fillers: list[str] = field(default_factory=list)  # lead-ins among spoken


@dataclass
class SpeakItem:
    message_id: str
    text: str
    turn: Turn | None = None
    is_hold: bool = False  # relay hold prompt: skipped if the answer arrived first
    is_filler: bool = False  # agent hold line before a Zigi call
    said_at: float = field(default_factory=time.monotonic)


@dataclass
class _State:
    name: str = "listening"
    user_speaking: bool = False
    last_partial: float = 0.0
    last_activity: float = field(default_factory=time.monotonic)


class VoiceSession:
    def __init__(self, ws: WebSocket, svc: Services, ctx: UserContext, mode: str = "relay", call_id: str = "") -> None:
        self.ws = ws
        self.call_id = call_id or uuid.uuid4().hex
        self.mode = mode if mode == "agent" and svc.agent is not None and svc.settings.agent_configured else "relay"
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
        self.last_final_at = 0.0
        self.speech_end_at: float | None = None
        self._send_lock = asyncio.Lock()
        self._closed = False
        # agent mode
        self.zigi = ZigiState()
        self.history: list[Any] = []  # the agent's message history, owned by the gateway between turns
        self.jobs: asyncio.Queue[tuple[str, Turn | None]] = asyncio.Queue()
        self.agent_task: asyncio.Task[None] | None = None
        self.push_buf: list[Message] = []
        self.zigi_seen: set[str] = set()  # Zigi messages the agent has already handled
        self.memory: dict[str, str] = {}  # what the caller told the agent about themselves (e.g. location)
        self.sends: list[dict[str, Any]] = []  # recent messages the agent sent Zigi (the service refuses repeats)
        self.agent_phase = "idle"  # idle | llm | zigi: what the running agent turn is doing
        self.agent_turn: Turn | None = None  # the turn the agent is working on
        self.followups: list[Turn] = []  # caller turns heard while Zigi was busy
        self.recent_spoken: list[tuple[float, str]] = []  # what we said lately, to drop echoes of it
        self.ignored_in_a_row = 0
        self.last_reply = ""  # what the agent last said (not lead-ins), sent with the next turn
        self.superseded = False  # the caller spoke over the agent's answer to Zigi's reply; it was not said
        self.held: tuple[Turn, asyncio.Task[None]] | None = None  # a number the caller is still dictating
        self.grounding: list[str] = []  # Zigi's and the caller's words this call: what numbers may come from

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
        if self.mode == "agent":
            await self._load_call_state()
        if self.stt_mode == "realtime":
            await self._open_realtime()
        await self.send(
            {
                "type": "ready",
                "conversation_id": self.ctx.conversation_id,
                "stt_mode": self.stt_mode,
                "sample_rate": self.cfg.tts_sample_rate,
                "mode": self.mode,
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
            if self.mode == "agent":
                tasks.append(asyncio.create_task(self._agent_worker(), name="agent"))
                if self.svc.agent is not None:
                    asyncio.create_task(self.svc.agent.warm())
            try:
                done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for t in done:
                    if t.exception() and not isinstance(t.exception(), WebSocketDisconnect):
                        log.exception("voice session task %s failed", t.get_name(), exc_info=t.exception())
            finally:
                self._closed = True
                if self.svc.limits is not None:
                    with contextlib.suppress(Exception):
                        await self.svc.limits.call_ended(self.ctx.user_id, self.call_id)
                for t in tasks:
                    t.cancel()
                if self.speaking:
                    self.speaking.cancel()
                if self.hold_task:
                    self.hold_task.cancel()
                if self.agent_task:
                    self.agent_task.cancel()
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
            if self.rt is not None:
                asyncio.create_task(self._force_commit(self.speech_end_at))
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
        if _HAS_WORD.search(text) and not _FILLER.match(text) and not self._is_echo(text):
            await self._user_text(text, source="voice")

    async def _force_commit(self, ended_at: float) -> None:
        """Noise can keep STT's own VAD from ending the utterance: commit when the client says speech ended."""
        await asyncio.sleep(self.cfg.stt_force_commit_s)
        if self.rt is None or self.state.user_speaking or self.last_final_at >= ended_at:
            return
        if time.monotonic() - self.state.last_partial > 3.0:
            return  # nothing pending: there is no utterance to commit
        with contextlib.suppress(Exception):
            await self.rt.commit()
            log.info("forced an STT commit after end of speech")

    async def _stt_loop(self) -> None:
        while True:
            assert self.rt is not None
            async for ev in self.rt.events():
                if ev.kind == "partial" and ev.text.strip():
                    self.state.last_partial = time.monotonic()
                    self.state.last_activity = self.state.last_partial
                    await self.send({"type": "transcript_partial", "text": ev.text})
                elif ev.kind == "final":
                    self.last_final_at = time.monotonic()
                    if _FILLER.match(ev.text):
                        log.info("ignored a filler-only transcript")
                        await self.send({"type": "transcript_partial", "text": ""})
                    elif self._is_echo(ev.text):
                        log.info("ignored a transcript of the assistant's own voice (echo)")
                        await self.send({"type": "transcript_partial", "text": ""})
                    elif _HAS_WORD.search(ev.text):
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
        if self.mode == "agent":
            await self._agent_user_turn(turn, source)
            return
        await self._interrupt(silent=True)
        await self.send({"type": "transcript_final", "text": text, "source": source, "turn_id": turn.id})
        await self.set_state("thinking")
        await self._relay_post(turn, source)

    async def _agent_user_turn(self, turn: Turn, source: str, release: bool = False) -> None:
        """Agentic mode: one turn at a time, so nothing reaches Zigi twice (seen 08 Nov: OTPs and questions sent twice
        when the caller spoke again, or a pause split a sentence, while the agent was still working)."""
        turn.route, turn.relay = "direct", False
        piece = turn.text
        if self.held is not None:
            earlier, timer = self.held
            self.held = None
            timer.cancel()
            turn.text = f"{earlier.text} {turn.text}"
            turn.t_speech_end = earlier.t_speech_end or turn.t_speech_end
        if not release and self._still_dictating(turn.text):
            # Seen 09 Nov: a pause in "026 226 0 ... 986" sent half a number to Zigi. Wait briefly for the rest.
            await self.send({"type": "transcript_final", "text": piece, "source": source, "turn_id": turn.id})
            self.held = (turn, asyncio.create_task(self._release_held(turn, source)))
            return
        if not await self._turn_allowed():
            return
        busy = self.agent_task is not None and not self.agent_task.done()
        await self._interrupt(silent=True, keep_agent=busy and self.agent_phase == "zigi")
        if not release:
            await self.send({"type": "transcript_final", "text": piece, "source": source, "turn_id": turn.id})
        await self.set_state("thinking")
        if busy and self.agent_phase == "zigi":
            # Zigi is already working on the last request: hear the answer first, then take this as the next turn.
            self.followups.append(turn)
            return
        if busy and self.agent_turn is not None and self.agent_turn.text and not self.agent_turn.spoken:
            # The agent was still thinking: this is more of the same utterance, so ask again with all of it.
            turn.text = f"{self.agent_turn.text} {turn.text}"
            turn.t_speech_end = turn.t_speech_end or self.agent_turn.t_speech_end
        self.jobs.put_nowait(("user", turn))

    def _still_dictating(self, text: str) -> bool:
        """Zigi wants a phone number or code and the caller has given only part of it so far."""
        waiting = self.zigi.waiting_for
        digits = spoken_digits(text)
        if waiting not in {"phone_number", "otp"} or not digits:
            return False
        if waiting == "otp":
            return len(digits) < 4
        if digits.startswith("233"):
            return len(digits) < 12
        return len(digits) < (10 if digits.startswith("0") else 9)

    async def _release_held(self, turn: Turn, source: str) -> None:
        await asyncio.sleep(self.cfg.agent_dictation_wait_s)
        if self.held is not None and self.held[0] is turn:
            self.held = None
            await self._agent_user_turn(turn, source, release=True)

    async def _turn_allowed(self) -> bool:
        if self.svc.limits is None:
            return True
        try:
            await self.svc.limits.hit(self.ctx.user_id, "voice_turns")
        except LimitExceeded:
            await self.send({"type": "notice", "code": "limit_voice_turns", "text": "That's a lot of turns in a minute. Give it a moment."})
            await self.set_state("listening")
            return False
        return True

    def _state_key(self) -> str:
        return f"callstate:{self.ctx.conversation_id}"

    async def _load_call_state(self) -> None:
        """Pick up the agent's memory of this conversation (survives reconnects, e.g. Vercel's 300 s limit)."""
        if self.svc.store is None:
            await self._zigi_state_from_sunco()
            return
        with contextlib.suppress(Exception):
            data = await self.svc.store.get_json(self._state_key())
            if data:
                self.history = data.get("history") or []
                self.zigi = state_from_dict(data.get("zigi_state"))
                self.grounding = list(data.get("grounding") or [])
                self.memory = dict(data.get("memory") or {})
                self.sends = list(data.get("sends") or [])
                self.zigi_seen = set(data.get("zigi_seen") or [])
                return
        await self._zigi_state_from_sunco()

    async def _zigi_state_from_sunco(self) -> None:
        """No saved state: read where Zigi left off from the conversation itself.

        Seen 09 Nov: a call started while Zigi was still waiting for a phone number from an earlier session (the
        saved state had expired), so the agent sent "I want to purchase" into Zigi's phone-number prompt.
        """
        try:
            messages = await self.svc.provider.list_messages(self.ctx.conversation_id)
        except Exception:  # noqa: BLE001
            return
        tail: list[Any] = []
        for m in reversed(messages):
            if m.author_type != "business":
                break
            if m.content.get("type") == "text" and not self.svc.speakable(m.text):
                continue
            tail.append(m)
        tail.reverse()
        if tail:
            self.zigi.update(tail)
            self.zigi_seen.update(m.id for m in messages)
            self.grounding.extend(m.text for m in tail if m.text)

    async def _save_call_state(self) -> None:
        if self.svc.store is None:
            return
        state = {
            "history": self.history,
            "zigi_state": state_to_dict(self.zigi),
            "grounding": self.grounding[-80:],
            "memory": self.memory,
            "sends": self.sends,
            "zigi_seen": sorted(self.zigi_seen)[-200:],
        }
        with contextlib.suppress(Exception):
            await self.svc.store.set_json(self._state_key(), state, self.cfg.call_state_ttl_s)

    async def _relay_post(self, turn: Turn, source: str) -> None:
        try:
            posted = await self.svc.provider.post_text(
                self.ctx.conversation_id,
                self.ctx.sunco_user_id,
                turn.text,
                {"channel": "voice", "source": "sc-voice-gateway", "input": source, "mode": self.mode},
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
            if self.mode == "agent" and not (turn and turn.relay):
                await self._agent_bot_message(msg)
                continue
            if self.mode == "agent":
                self.zigi.update((self.zigi.messages + [msg])[-6:])
                self.grounding.append(msg.text)
            if turn and turn.t_bot_any is None:
                turn.t_bot_any = time.monotonic()
            speech = render(msg.content)
            spoken = self.svc.speakable(msg.text) and bool(speech)
            await self.send({"type": "bot_message", "message": msg.to_dict(), "speech": speech if spoken else ""})
            if spoken:
                if turn and turn.t_bot_first is None:
                    turn.t_bot_first = time.monotonic()
                self.queue.put_nowait(SpeakItem(msg.id, speech, turn))

    async def _agent_bot_message(self, msg: Message) -> None:
        """Agent mode: Zigi's message is shown in the chat; the agent service reads Zigi itself.

        A message the agent has not seen and that arrives while no turn runs (e.g. the reply to a photo) is pushed
        to the agent, which tells the caller.
        """
        await self.send({"type": "bot_message", "message": msg.to_dict(), "speech": ""})
        busy = (self.agent_task is not None and not self.agent_task.done()) or not self.jobs.empty()
        if busy or msg.id in self.zigi_seen:
            return
        if not self.push_buf:
            self.jobs.put_nowait(("push", None))
        self.push_buf.append(msg)

    # ------------------------------------------------------------------ agent mode (the agent service)
    async def _agent_worker(self) -> None:
        while True:
            kind, turn = await self.jobs.get()
            if kind == "push" and not self.jobs.empty():
                continue  # a caller turn follows
            self.agent_task = asyncio.create_task(self._agent_job(kind, turn))
            await asyncio.wait({self.agent_task})
            self.agent_task = None
            self.agent_phase = "idle"
            if self.followups:
                first, *rest = self.followups
                self.followups = []
                first.text = " ".join(t.text for t in [first, *rest])
                if self.superseded:
                    first.note = (
                        "The caller spoke again while you were waiting for ZIGI, so your previous reply was NOT said "
                        "aloud. Respond to what they say now; use ZIGI's last reply only where it still answers them."
                    )
                self.superseded = False
                self.turn = first
                self.jobs.put_nowait(("user", first))
                continue
            if self.queue.empty() and not (self.speaking and not self.speaking.done()) and self.jobs.empty():
                await self.set_state("listening")

    async def _agent_job(self, kind: str, turn: Turn | None) -> None:
        if kind == "user" and turn is not None:
            await self._agent_turn(turn, "user")
            return
        while True:  # let Zigi's burst finish
            n = len(self.push_buf)
            await asyncio.sleep(self.cfg.agent_quiet_s)
            if len(self.push_buf) == n:
                break
        burst = [
            m
            for m in self.push_buf
            if m.id not in self.zigi_seen and (self.svc.speakable(m.text) or m.content.get("type") in {"image", "file"})
        ]
        self.push_buf = []
        if not burst:
            return
        self.zigi.update(burst)
        self.zigi_seen.update(m.id for m in burst)
        self.grounding.extend(m.text for m in burst)
        push = Turn(id=uuid.uuid4().hex[:12], text="", t_speech_end=None, t_final=time.monotonic(), route="push", relay=False)
        self.turn = push
        await self.set_state("thinking")
        await self._agent_turn(push, "push")

    async def _agent_turn(self, turn: Turn, kind: str) -> None:
        self.agent_turn, self.agent_phase = turn, "llm"
        if kind == "user" and self.ignored_in_a_row >= 2 and not turn.note:
            turn.note = (
                "You stayed silent for the last two things you heard. If this could be meant for you at all, "
                "answer it; the caller may be repeating themselves."
            )
        if self.cfg.log_conversation_text and kind == "user":
            logfire.info("caller said {text}", text=turn.text, turn_id=turn.id)
        request = {
            "conversation_id": self.ctx.conversation_id,
            "sunco_user_id": self.ctx.sunco_user_id,
            "kind": kind,
            "text": turn.text,
            "history": self.history,
            "zigi_state": state_to_dict(self.zigi),
            "grounding": self.grounding[-80:],
            "memory": self.memory,
            "sends": self.sends,
            "note": turn.note,
            "last_reply": self.last_reply,
        }
        assert self.svc.agent is not None
        stream = self.svc.agent.turn(request)
        failed = False
        try:
            while True:
                timeout = self.cfg.agent_first_event_timeout_s if not turn.spoken else 60
                try:
                    event = await asyncio.wait_for(anext(stream), timeout)
                except StopAsyncIteration:
                    break
                await self._on_agent_event(turn, event)
                if event["type"] == "error":
                    failed = True
        except (AgentServiceError, TimeoutError) as exc:
            log.warning("agent service turn failed: %s", type(exc).__name__)
            failed = True
        finally:
            with contextlib.suppress(Exception):
                await stream.aclose()
        if failed and not turn.spoken and kind == "user" and not turn.t_posted:
            turn.route, turn.relay = "fallback", True
            await self._relay_post(turn, "voice")

    async def _on_agent_event(self, turn: Turn, event: dict[str, Any]) -> None:
        kind = event.get("type")
        if kind == "say":
            text = str(event.get("text") or "")
            speech = render({"type": "text", "text": text})
            if turn.t_llm_first is None:
                turn.t_llm_first = time.monotonic()
            if not speech:
                return
            filler = bool(event.get("filler"))
            if self.followups and not filler and turn.t_tool_start is not None:
                # The caller has already said something newer: don't answer the older request out loud.
                self.superseded = True
                return
            turn.spoken.append(text)
            if filler:
                turn.fillers.append(text)
            if self.cfg.log_conversation_text:
                logfire.info("agent said {text}", text=text, turn_id=turn.id, filler=filler)
            turn.filler_spoken |= filler
            await self.send({"type": "agent_text", "turn_id": turn.id, "text": text, "filler": filler})
            item_id = f"{'hold' if filler else 'agent'}-{turn.id}-{len(turn.spoken)}"
            self.queue.put_nowait(SpeakItem(item_id, speech, turn, is_filler=filler))
        elif kind == "tool":
            if event.get("name") in {"ask_zigi", "choose_option", "web_fetch"}:
                if event.get("phase") == "start":
                    self.agent_phase = "zigi"
                    turn.t_tool_start = turn.t_tool_start or time.monotonic()
                else:
                    self.agent_phase = "llm"
                    turn.t_tool_end = time.monotonic()
        elif kind == "zigi_posted":
            message = event["message"]
            self.seen.add(message["id"])
            turn.route = "zigi" if turn.route != "push" else turn.route
            if turn.t_posted is None:
                turn.t_posted = time.monotonic()
            await self.send({"type": "user_message", "message": message})
        elif kind == "done":
            self.history = event.get("history") or []
            self.zigi = state_from_dict(event.get("zigi_state"))
            self.grounding = list(event.get("grounding") or [])[-80:]
            self.zigi_seen.update(event.get("zigi_seen") or [])
            self.memory = dict(event.get("memory") or self.memory)
            self.sends = list(event.get("sends") or self.sends)
            await self._save_call_state()
            turn.zigi_ms = (event.get("zigi_ms") or 0) / 1000
            turn.route = event.get("route") or turn.route
            turn.done = True
            answer = [t for t in turn.spoken if t not in turn.fillers]
            if answer:
                self.last_reply = " ".join(answer)
            if turn.route == "ignored":
                # The agent judged this wasn't said to it (someone else in the room): say nothing, keep listening.
                self.ignored_in_a_row += 1
                await self.send({"type": "turn_ignored", "turn_id": turn.id})
                return
            self.ignored_in_a_row = 0
            await self._report(turn)

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
                await self.set_state("thinking" if self._awaiting_reply() else "listening")

    def _awaiting_reply(self) -> bool:
        if self.agent_task is not None and not self.agent_task.done():
            return True
        t = self.turn
        return t is not None and t.relay and t.t_bot_first is None and t.t_bot_any is None

    def _is_echo(self, text: str) -> bool:
        """The transcript is the assistant's own recent words picked up by the mic (seen 09 Nov on an iPhone
        speaker: "What else can I help with?" was sent to Zigi as if the caller said it)."""
        words = _WORDS.findall(text.lower())
        if len(words) < 3:
            return False
        now = time.monotonic()
        for at, said in self.recent_spoken:
            if now - at > 30:
                continue
            spoken = set(_WORDS.findall(said.lower()))
            if sum(w in spoken for w in words) / len(words) >= 0.8:
                return True
        return False

    async def _speak(self, item: SpeakItem) -> None:
        self.recent_spoken = [*self.recent_spoken[-7:], (time.monotonic(), item.text)]
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
            # Short sentences repeat (lead-ins, holds, "anything else?"): cache their audio, skip the TTS round trip.
            key = None
            if len(text) <= 120:
                norm = " ".join(text.lower().split())
                key = "s:" + hashlib.sha256(f"{self.cfg.tts_voice_id}|{self.cfg.tts_model_voice}|{fmt}|{norm}".encode()).hexdigest()[:32]
            try:
                hit = None
                if key:
                    with contextlib.suppress(Exception):  # a cache outage must not stop speech
                        hit = await self.svc.cache.get(key)
                if hit:
                    if item.turn is not None:
                        item.turn.tts_cache_hits += 1
                    out.put_nowait(hit[0])
                    return
                buf = bytearray()
                async for chunk in self.svc.tts.stream(text, self.cfg.tts_model_voice, fmt):
                    out.put_nowait(chunk)
                    buf += chunk
                if key and buf:
                    with contextlib.suppress(Exception):
                        await self.svc.cache.put(key, bytes(buf), "audio/pcm")
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
                        if item.turn and item.turn.t_audio_first is None:
                            item.turn.t_audio_first = time.monotonic()
                        if (
                            item.turn
                            and not item.turn.relay
                            and not item.is_hold
                            and not any(m[0] == item.said_at for m in item.turn.audio_marks)
                        ):
                            item.turn.audio_marks.append((item.said_at, time.monotonic()))
                            await self._report(item.turn)
                        if item.turn and item.turn.t_tts_first is None and not item.is_hold and not item.is_filler:
                            item.turn.t_tts_first = time.monotonic()
                            await self._report(item.turn)
                        await self.send_audio(data[:cut])
            await self.send({"type": "audio_end", "message_id": item.message_id})
        finally:
            for f in fetchers:
                f.cancel()

    async def _interrupt(self, silent: bool = False, keep_agent: bool | None = None) -> None:
        while not self.queue.empty():
            self.queue.get_nowait()
        if keep_agent is None:
            keep_agent = self.agent_phase == "zigi"  # a barge-in never abandons a request Zigi is working on
        if self.agent_task and not self.agent_task.done() and not keep_agent:
            self.agent_task.cancel()
        if self.speaking and not self.speaking.done():
            self.speaking.cancel()
            await self.send({"type": "audio_stop"})
        if not silent:
            await self.set_state("listening")

    async def _report(self, turn: Turn) -> None:
        if turn.reported:
            return
        if not turn.relay:
            await self._report_agent(turn)
            return
        if turn.t_tts_first is None or turn.t_posted is None or turn.t_bot_first is None:
            return
        turn.reported = True

        def ms(a: float | None, b: float | None) -> int | None:
            return None if a is None or b is None else round((b - a) * 1000)

        start = turn.t_speech_end if turn.t_speech_end is not None else turn.t_final
        total = ms(start, turn.t_tts_first) or 0
        if turn.relay:
            bot_ms = ms(turn.t_posted, turn.t_bot_first) or 0
            extra: dict[str, Any] = {
                "post_ms": ms(turn.t_final, turn.t_posted),
                "tts_first_byte_ms": ms(turn.t_bot_first, turn.t_tts_first),
            }
        else:
            bot_ms = round(turn.zigi_ms * 1000)
            extra = {
                "llm_ms": ms(turn.t_final, turn.t_llm_first),
                "first_audio_ms": ms(start, turn.t_audio_first),
            }
        metrics = {
            "type": "metrics",
            "turn_id": turn.id,
            "tts_cache_hits": turn.tts_cache_hits,
            "mode": self.mode,
            "route": turn.route,
            "stt_ms": ms(turn.t_speech_end, turn.t_final),
            "bot_ms": bot_ms,
            "gateway_ms": total - bot_ms,
            "total_ms": total,
            **extra,
        }
        telemetry.turn_metrics(metrics)
        await self.send(metrics)

    async def _report_agent(self, turn: Turn) -> None:
        """Agentic turns are reported once the route is known and the answer is audible.

        first_audio_ms: end of speech to the first sound (a lead-in counts). total_ms: to the answer itself, i.e. the
        first sentence said after the last Zigi call (or the first sentence when there was none).
        """
        if not turn.done or not turn.audio_marks:
            return
        after = turn.t_tool_end or 0.0
        answer = next((audio for said, audio in turn.audio_marks if said >= after), None)
        if answer is None:
            return
        turn.reported = True

        def ms(a: float | None, b: float | None) -> int | None:
            return None if a is None or b is None else round((b - a) * 1000)

        start = turn.t_speech_end if turn.t_speech_end is not None else turn.t_final
        total = ms(start, answer) or 0
        zigi = round(turn.zigi_ms * 1000)
        metrics = {
            "type": "metrics",
            "turn_id": turn.id,
            "tts_cache_hits": turn.tts_cache_hits,
            "mode": self.mode,
            "route": turn.route,
            "stt_ms": ms(turn.t_speech_end, turn.t_final),
            "llm_ms": ms(turn.t_final, turn.t_llm_first),
            "bot_ms": zigi,
            "first_audio_ms": ms(start, min(a for _, a in turn.audio_marks)),
            "gateway_ms": total - zigi,
            "total_ms": total,
        }
        telemetry.turn_metrics(metrics)
        await self.send(metrics)

    async def _watchdog(self) -> None:
        started = last_beat = time.monotonic()
        while True:
            await asyncio.sleep(5)
            now = time.monotonic()
            if self.svc.limits is not None and now - last_beat >= 30:
                try:
                    await self.svc.limits.heartbeat(self.ctx.user_id, self.call_id, now - last_beat)
                except LimitExceeded:
                    await self.send({"type": "session_end", "reason": "voice_minutes"})
                    return
                last_beat = now
            if now - started > self.cfg.session_max_s:
                await self.send({"type": "session_end", "reason": "max_duration"})
                return
            busy = self.state.name != "listening" or self.state.user_speaking
            if not busy and now - self.state.last_activity > self.cfg.idle_timeout_s:
                await self.send({"type": "session_end", "reason": "idle"})
                return
            if busy:
                self.state.last_activity = now
