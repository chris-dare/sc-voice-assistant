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
import json
import logging
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

import logfire
from fastapi import WebSocket, WebSocketDisconnect

from . import telemetry
from .agent import (
    SYSTEM_PROMPT,
    TOOLS,
    LLMError,
    ZigiState,
    is_affirmative,
    mask_digits,
    shape_input,
    state_prompt,
    tool_result,
    ungrounded_numbers,
)
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
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")
_UNGROUNDED_NOTE = "Your reply contained numbers or codes that did not come from ZIGI. Do not answer MTN facts yourself: call ask_zigi."
_PUSH_NOTE = (
    "ZIGI just sent the messages shown in ZIGI STATE without a new question from the customer "
    "(for example a reply to a photo or file). Tell the customer briefly what ZIGI said."
)


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
    filler_spoken: bool = False
    spoken: list[str] = field(default_factory=list)


@dataclass
class SpeakItem:
    message_id: str
    text: str
    turn: Turn | None = None
    is_hold: bool = False  # relay hold prompt: skipped if the answer arrived first
    is_filler: bool = False  # agent hold line before a Zigi call


@dataclass
class _State:
    name: str = "listening"
    user_speaking: bool = False
    last_partial: float = 0.0
    last_activity: float = field(default_factory=time.monotonic)


class VoiceSession:
    def __init__(self, ws: WebSocket, svc: Services, ctx: UserContext, mode: str = "relay") -> None:
        self.ws = ws
        self.mode = mode if mode == "agent" and svc.llm is not None else "relay"
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
        # agent mode
        self.zigi = ZigiState()
        self.history: list[dict[str, Any]] = []
        self.jobs: asyncio.Queue[tuple[str, Turn | None]] = asyncio.Queue()
        self.agent_task: asyncio.Task[None] | None = None
        self.tool_inbox: asyncio.Queue[Message] | None = None
        self.push_buf: list[Message] = []
        self.grounding: list[str] = []  # Zigi's and the user's words this call: what numbers may come from

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
        if self.mode == "agent":
            turn.route, turn.relay = "direct", False
            self.grounding.append(text)
            self.jobs.put_nowait(("user", turn))
            return
        await self._relay_post(turn, source)

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
            if self.mode == "agent":
                self.grounding.append(msg.text)
                self.grounding.extend(str(a.get("text", "")) for a in msg.content.get("actions") or [])
                if not (turn and turn.relay):
                    await self._agent_bot_message(msg)
                    continue
                self.zigi.update((self.zigi.messages + [msg])[-6:])
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
        """Agent mode: Zigi's message is data for the agent, shown in the chat but not spoken as is."""
        await self.send({"type": "bot_message", "message": msg.to_dict(), "speech": ""})
        if self.tool_inbox is not None:
            self.tool_inbox.put_nowait(msg)
            return
        busy = (self.agent_task is not None and not self.agent_task.done()) or not self.jobs.empty()
        if busy:
            # A user turn is in progress: its next look at ZIGI STATE includes this message.
            self.zigi.update((self.zigi.messages + [msg])[-6:])
            return
        # Unprompted (e.g. the reply to an attachment): let the agent tell the user.
        if not self.push_buf:
            self.jobs.put_nowait(("zigi", None))
        self.push_buf.append(msg)

    # ------------------------------------------------------------------ agent mode
    async def _agent_worker(self) -> None:
        while True:
            kind, turn = await self.jobs.get()
            if kind == "zigi" and not self.jobs.empty():
                continue  # a user turn follows; it sees the update in ZIGI STATE
            self.agent_task = asyncio.create_task(self._agent_job(kind, turn))
            await asyncio.wait({self.agent_task})
            self.agent_task = None
            if self.queue.empty() and not (self.speaking and not self.speaking.done()) and self.jobs.empty():
                await self.set_state("listening")

    async def _agent_job(self, kind: str, turn: Turn | None) -> None:
        if kind == "user" and turn is not None:
            await self._agent_turn(turn, [{"role": "user", "content": turn.text}])
            return
        # Zigi pushed messages without a question: wait for the burst to finish, then relay it through the agent.
        while True:
            n = len(self.push_buf)
            await asyncio.sleep(self.cfg.agent_quiet_s)
            if len(self.push_buf) == n:
                break
        burst = [m for m in self.push_buf if self.svc.speakable(m.text) or m.content.get("type") in {"image", "file"}]
        self.push_buf = []
        if not burst:
            return
        self.zigi.update(burst)
        push = Turn(id=uuid.uuid4().hex[:12], text="", t_speech_end=None, t_final=time.monotonic(), route="push", relay=False)
        self.turn = push
        await self.set_state("thinking")
        await self._agent_turn(push, [{"role": "system", "content": _PUSH_NOTE}])

    def _agent_context(self) -> list[dict[str, Any]]:
        keep = self.cfg.agent_history_turns * 2
        return [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "system", "content": state_prompt(self.zigi)},
            *self.history[-keep:],
        ]

    async def _agent_turn(self, turn: Turn, opening: list[dict[str, Any]]) -> None:
        """One agent turn: up to four LLM rounds, with Zigi tool calls in between (design 5.8.1)."""
        assert self.svc.llm is not None
        msgs = self._agent_context() + opening
        tool_choice: Any = None
        used_tool = False
        try:
            for rnd in range(4):
                text, calls, ungrounded = await self._llm_round(turn, msgs, rnd, tool_choice, strict=not used_tool)
                tool_choice = None
                if ungrounded:
                    if used_tool or rnd >= 2:
                        if not turn.spoken:
                            await self._speak_raw(turn)  # nothing usable said: give Zigi's own words
                        return
                    log.warning("agent reply had numbers not from Zigi; asking Zigi instead")
                    msgs.append(
                        {
                            "role": "system",
                            "content": _UNGROUNDED_NOTE,
                        }
                    )
                    tool_choice = {"type": "function", "function": {"name": "ask_zigi"}}
                    continue
                if not calls:
                    return
                used_tool = True
                turn.route = "zigi" if turn.route != "push" else turn.route
                msgs.append(
                    {
                        "role": "assistant",
                        "content": text or None,
                        "tool_calls": [
                            {
                                "id": c.id,
                                "type": "function",
                                "function": {"name": c.name, "arguments": c.raw_arguments or json.dumps(c.arguments)},
                            }
                            for c in calls
                        ],
                    }
                )
                if not turn.filler_spoken and not text.strip() and self.cfg.agent_hold_line:
                    turn.filler_spoken = True
                    self.queue.put_nowait(SpeakItem(f"hold-{turn.id}", self.cfg.agent_hold_line, turn, is_filler=True))
                for call in calls:
                    msgs.append({"role": "tool", "tool_call_id": call.id, "content": await self._run_tool(turn, call)})
        except (LLMError, TimeoutError) as exc:
            log.warning("agent turn failed (%s): %s", type(exc).__name__, exc)
            if turn.spoken:
                return
            if used_tool or turn.route == "push":
                await self._speak_raw(turn)
            else:
                turn.route, turn.relay = "fallback", True
                await self._relay_post(turn, "voice")
        finally:
            said = " ".join(turn.spoken)
            if turn.text:
                self.history.append({"role": "user", "content": turn.text})
            if said:
                self.history.append({"role": "assistant", "content": said})

    async def _llm_round(
        self, turn: Turn, msgs: list[dict[str, Any]], rnd: int, tool_choice: Any, strict: bool
    ) -> tuple[str, list[Any], bool]:
        """Stream one LLM reply into speech. strict: an unsupported number ends the round (the agent must ask Zigi);
        otherwise (after a Zigi call) only that sentence is dropped."""
        assert self.svc.llm is not None
        buf, text = "", ""
        calls: list[Any] = []
        stream = self.svc.llm.stream(msgs, TOOLS, tool_choice)
        first = True
        with logfire.span("agent llm round {round}", round=rnd, model=self.svc.llm.model, turn_id=turn.id) as span:
            try:
                while True:
                    try:
                        ev = await asyncio.wait_for(anext(stream), self.cfg.agent_first_token_timeout_s if first else 20)
                    except StopAsyncIteration:
                        break
                    if first:
                        first = False
                        if turn.t_llm_first is None:
                            turn.t_llm_first = time.monotonic()
                    if ev.kind == "text":
                        buf += ev.text
                        text += ev.text
                        *done, buf = _SENTENCE_END.split(buf)
                        for sentence in done:
                            if not await self._agent_say(turn, sentence):
                                span.set_attribute("ungrounded", True)
                                if strict:
                                    return text, [], True
                    elif ev.kind == "tool_calls":
                        calls = ev.calls
            finally:
                with contextlib.suppress(Exception):
                    await stream.aclose()
            if buf.strip() and not await self._agent_say(turn, buf):
                span.set_attribute("ungrounded", True)
                if strict:
                    return text, [], True
            span.set_attribute("tools", [c.name for c in calls])
        return text, calls, False

    async def _agent_say(self, turn: Turn, sentence: str) -> bool:
        sentence = sentence.strip()
        if not sentence:
            return True
        if missing := ungrounded_numbers(sentence, self.grounding):
            log.warning("dropped an agent sentence with ungrounded numbers %s", missing)
            return False
        speech = render({"type": "text", "text": sentence})
        if not speech:
            return True
        turn.spoken.append(sentence)
        await self.send({"type": "agent_text", "turn_id": turn.id, "text": sentence})
        self.queue.put_nowait(SpeakItem(f"agent-{turn.id}-{len(turn.spoken)}", speech, turn))
        return True

    async def _speak_raw(self, turn: Turn) -> None:
        """Fallback: say Zigi's last reply as relay mode would."""
        turn.route = "fallback"
        for m in self.zigi.messages:
            speech = render(m.content)
            if self.svc.speakable(m.text) and speech:
                turn.spoken.append(m.text)
                await self.send({"type": "agent_text", "turn_id": turn.id, "text": m.text})
                self.queue.put_nowait(SpeakItem(m.id, speech, turn))

    async def _run_tool(self, turn: Turn, call: Any) -> str:
        waiting = self.zigi.waiting_for
        with logfire.span("tool {name}", name=call.name, waiting_for=waiting, turn_id=turn.id) as span:
            content, refusal = self._tool_content(turn, call, waiting)
            if refusal:
                span.set_attribute("refused", True)
                return json.dumps({"error": refusal})
            assert content is not None
            t0 = time.monotonic()
            self.tool_inbox = asyncio.Queue()
            try:
                try:
                    posted = await self.svc.provider.post_content(
                        self.ctx.conversation_id,
                        self.ctx.sunco_user_id,
                        content,
                        {"channel": "voice", "source": "sc-voice-gateway", "input": "agent", "mode": "agent"},
                    )
                except SunCoError as exc:
                    log.warning("agent post to SunCo failed: %s", exc)
                    return json.dumps({"error": "ZIGI can't be reached right now. Tell the user to try again shortly."})
                if turn.t_posted is None:
                    turn.t_posted = time.monotonic()
                self.seen.add(posted.id)
                self.grounding.append(content.get("text", ""))
                await self.send({"type": "user_message", "message": posted.to_dict()})
                burst = await self._collect_burst(turn)
            finally:
                self.tool_inbox = None
                turn.zigi_ms += time.monotonic() - t0
            if burst:
                self.zigi.update(burst)
            span.set_attribute("messages", len(burst))
            span.set_attribute("now_waiting_for", self.zigi.waiting_for)
            return tool_result(burst, self.zigi.waiting_for, timed_out=not burst)

    def _tool_content(self, turn: Turn, call: Any, waiting: str) -> tuple[dict[str, Any] | None, str | None]:
        if call.name == "ask_zigi":
            text, refusal = shape_input(str(call.arguments.get("text") or ""), waiting, turn.text)
            if refusal or text is None:
                return None, refusal or "Nothing to send."
            if waiting in {"otp", "phone_number"}:
                log.info("agent sends %s %s", waiting, mask_digits(text))
            return {"type": "text", "text": text[:2000]}, None
        if call.name == "choose_option":
            want = str(call.arguments.get("option") or "").strip().lower()
            options = self.zigi.options
            pick = next((a for a in options if str(a["text"]).strip().lower() == want), None) or next(
                (a for a in options if want and want in str(a["text"]).lower()), None
            )
            if pick is None:
                names = ", ".join(str(a["text"]) for a in options) or "none"
                return None, f"That is not one of ZIGI's options. The options are: {names}."
            if waiting == "confirmation" and is_affirmative(str(pick["text"])) and not is_affirmative(turn.text):
                return None, "The user has not said yes in this turn. Read ZIGI's question to the user and wait for their answer."
            content: dict[str, Any] = {"type": "text", "text": str(pick["text"])}
            if pick.get("payload"):
                content["payload"] = pick["payload"]
            return content, None
        return None, f"Unknown tool {call.name}."

    async def _collect_burst(self, turn: Turn) -> list[Message]:
        """Zigi's reply: every message until a quiet gap after the first real one, or the bot timeout."""
        assert self.tool_inbox is not None
        burst: list[Message] = []
        deadline = time.monotonic() + self.cfg.bot_timeout_s
        while True:
            wait = self.cfg.agent_quiet_s if burst else deadline - time.monotonic()
            if wait <= 0:
                break
            try:
                msg = await asyncio.wait_for(self.tool_inbox.get(), wait)
            except TimeoutError:
                break
            if turn.t_bot_any is None:
                turn.t_bot_any = time.monotonic()
            if msg.content.get("type") == "text" and not self.svc.speakable(msg.text):
                continue  # debug lines from the dev bot
            if turn.t_bot_first is None:
                turn.t_bot_first = time.monotonic()
            burst.append(msg)
        return burst

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
                        if item.turn and item.turn.t_audio_first is None:
                            item.turn.t_audio_first = time.monotonic()
                        if item.turn and item.turn.t_tts_first is None and not item.is_hold and not item.is_filler:
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
        if self.agent_task and not self.agent_task.done():
            self.agent_task.cancel()
        if self.speaking and not self.speaking.done():
            self.speaking.cancel()
            await self.send({"type": "audio_stop"})
        if not silent:
            await self.set_state("listening")

    async def _report(self, turn: Turn) -> None:
        if turn.reported or turn.t_tts_first is None:
            return
        if turn.relay and (turn.t_posted is None or turn.t_bot_first is None):
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
