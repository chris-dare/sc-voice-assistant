"""One agent turn as a stream of events for the voice gateway.

Events (one JSON object per line on the wire):
  {"type": "say", "text", "filler"?}     a sentence to speak now (filler: a lead-in before a Zigi call)
  {"type": "zigi_posted", "message"}     what the agent sent Zigi, as stored in SunCo
  {"type": "tool", "name", "phase"}      a tool started or finished
  {"type": "done", "route", "history", "zigi_state", "grounding", "zigi_seen", "llm_first_ms", "zigi_ms", "model"}
  {"type": "error", "code", "text"}      the turn failed before anything useful was said
"""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import AsyncIterator
from typing import Any

import logfire
from pydantic_ai import Agent, ModelMessagesTypeAdapter
from pydantic_ai.exceptions import AgentRunError, ModelAPIError, ModelRequestTimeout
from pydantic_ai.messages import (
    FunctionToolCallEvent,
    FunctionToolResultEvent,
    PartDeltaEvent,
    PartEndEvent,
    PartStartEvent,
    TextPart,
    TextPartDelta,
    ToolCallPart,
)
from pydantic_ai.run import AgentRunResultEvent

from .agent import CallDeps
from .persona import PUSH_NOTE, lead_in
from .rules import state_to_dict, ungrounded_numbers

_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")
_UNGROUNDED_NOTE = (
    "Your last draft said numbers or codes that did not come from ZIGI. Don't answer MTN facts yourself: "
    "say a short lead-in, then call ask_zigi."
)
ZIGI_TOOLS = {"ask_zigi", "choose_option"}


class _Ungrounded(Exception):
    pass


async def run_turn(
    agent: Agent[CallDeps, str], deps: CallDeps, *, kind: str, text: str, history: list[Any]
) -> AsyncIterator[dict[str, Any]]:
    queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()

    async def emit(event: dict[str, Any]) -> None:
        queue.put_nowait(event)

    deps.emit = emit
    task = asyncio.create_task(_drive(agent, deps, kind=kind, text=text, history=history, emit=emit, done=queue))
    try:
        while (event := await queue.get()) is not None:
            yield event
    finally:
        if not task.done():
            task.cancel()


async def _drive(
    agent: Agent[CallDeps, str],
    deps: CallDeps,
    *,
    kind: str,
    text: str,
    history: list[Any],
    emit: Any,
    done: asyncio.Queue[dict[str, Any] | None],
) -> None:
    t0 = time.monotonic()
    state: dict[str, Any] = {"said": 0, "first": None, "last_lead": ""}
    messages = ModelMessagesTypeAdapter.validate_python(history) if history else []
    prompt: str | None = text if kind == "user" else None
    extra: list[str] = [PUSH_NOTE] if kind == "push" else []
    if kind == "user":
        deps.grounding.append(text)
    try:
        with logfire.span("agent turn {kind}", kind=kind) as span:
            for attempt in range(2):
                try:
                    result = await _stream(agent, deps, prompt, messages, extra, emit, state, t0)
                    break
                except _Ungrounded:
                    if attempt:
                        raise
                    logfire.warn("agent draft had ungrounded numbers; asking Zigi instead")
                    extra = [*extra, _UNGROUNDED_NOTE]
            route = "push" if kind == "push" else ("zigi" if deps.used_zigi else "cache" if deps.cache_hits else "direct")
            span.set_attribute("route", route)
            await emit(
                {
                    "type": "done",
                    "route": route,
                    "history": ModelMessagesTypeAdapter.dump_python(result.all_messages(), mode="json"),
                    "zigi_state": state_to_dict(deps.zigi),
                    "grounding": deps.grounding[-80:],
                    "zigi_seen": sorted(deps.zigi_seen),
                    "memory": deps.memory,
                    "sends": deps.sends,
                    "llm_first_ms": state["first"],
                    "zigi_ms": round(deps.zigi_ms * 1000),
                    "model": getattr(result.response, "model_name", None),
                }
            )
    except (ModelRequestTimeout, ModelAPIError, AgentRunError, _Ungrounded) as exc:
        logfire.warn("agent turn failed: {error}", error=type(exc).__name__)
        await emit({"type": "error", "code": "agent_failed", "text": type(exc).__name__, "said": state["said"]})
    finally:
        done.put_nowait(None)


async def _stream(agent, deps, prompt, messages, extra, emit, state, t0):  # noqa: ANN001, ANN202
    buf = ""

    async def say(sentence: str, *, filler: bool = False) -> None:
        sentence = sentence.strip()
        if not sentence:
            return
        if not filler and (missing := ungrounded_numbers(sentence, deps.grounding)):
            logfire.warn("dropped an agent sentence with ungrounded numbers {missing}", missing=missing)
            if not deps.used_zigi:
                raise _Ungrounded()
            return
        if state["first"] is None:
            state["first"] = round((time.monotonic() - t0) * 1000)
        state["said"] += 1
        await emit({"type": "say", "text": sentence, **({"filler": True} if filler else {})})

    async def flush() -> None:
        nonlocal buf
        rest, buf = buf, ""
        await say(rest)

    result = None
    instructions = "\n\n".join(extra) or None
    async with agent.run_stream_events(prompt, deps=deps, message_history=messages, instructions=instructions) as events:
        async for ev in events:
            if isinstance(ev, PartStartEvent) and isinstance(ev.part, TextPart):
                buf += ev.part.content
            elif isinstance(ev, PartDeltaEvent) and isinstance(ev.delta, TextPartDelta):
                buf += ev.delta.content_delta
            elif isinstance(ev, PartEndEvent) and isinstance(ev.part, TextPart):
                await flush()
                continue
            elif isinstance(ev, PartStartEvent) and isinstance(ev.part, ToolCallPart):
                await flush()
                if state["said"] == 0 and ev.part.tool_name in ZIGI_TOOLS | {"web_fetch"}:
                    line = lead_in(state["last_lead"])
                    state["last_lead"] = line
                    await say(line, filler=True)
                continue
            elif isinstance(ev, FunctionToolCallEvent):
                await emit({"type": "tool", "name": ev.part.tool_name, "phase": "start"})
                continue
            elif isinstance(ev, FunctionToolResultEvent):
                await emit({"type": "tool", "name": ev.part.tool_name, "phase": "end"})
                continue
            elif isinstance(ev, AgentRunResultEvent):
                result = ev.result
                continue
            else:
                continue
            *complete, buf = _SENTENCE_END.split(buf)
            for sentence in complete:
                await say(sentence)
    await flush()
    assert result is not None
    return result
