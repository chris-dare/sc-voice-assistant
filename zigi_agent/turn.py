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
    PartStartEvent,
    TextPart,
    TextPartDelta,
    ToolCallPart,
)
from pydantic_ai.run import AgentRunResultEvent

from .agent import CallDeps
from .persona import PUSH_NOTE, lead_in
from .rules import asks_about_mtn, state_to_dict, ungrounded_numbers

_SKIP = re.compile(r"^\W*skip\W*$", re.I)
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")
_UNGROUNDED_NOTE = (
    "Your last draft said numbers or codes that did not come from ZIGI. Don't answer MTN facts yourself: "
    "say a short lead-in, then call ask_zigi."
)
ZIGI_TOOLS = {"ask_zigi", "choose_option"}


class _Ungrounded(Exception):
    pass


async def run_turn(
    agent: Agent[CallDeps, str], deps: CallDeps, *, kind: str, text: str, history: list[Any], note: str = ""
) -> AsyncIterator[dict[str, Any]]:
    queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()

    async def emit(event: dict[str, Any]) -> None:
        queue.put_nowait(event)

    deps.emit = emit
    task = asyncio.create_task(_drive(agent, deps, kind=kind, text=text, history=history, emit=emit, done=queue, note=note))
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
    note: str = "",
) -> None:
    t0 = time.monotonic()
    state: dict[str, Any] = {"said": 0, "first": None, "last_lead": "", "note": bool(note)}
    messages = ModelMessagesTypeAdapter.validate_python(history) if history else []
    prompt: str | None = text if kind == "user" else None
    extra: list[str] = [PUSH_NOTE] if kind == "push" else []
    if note:
        extra.append(note[:500])
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
                    state["retried"] = True
                    logfire.warn("agent draft had ungrounded numbers; asking Zigi instead")
                    extra = [*extra, _UNGROUNDED_NOTE]
            route = "push" if kind == "push" else ("zigi" if deps.used_zigi else "cache" if deps.cache_hits else "direct")
            if state.get("skipped") and not state["said"] and route == "direct":
                route = "ignored"
                logfire.info("agent ignored speech that was not meant for it")
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
                    "refusals": deps.refusals,
                    "refused": deps.refused,
                    "model": getattr(result.response, "model_name", None),
                }
            )
    except (ModelRequestTimeout, ModelAPIError, AgentRunError, _Ungrounded) as exc:
        logfire.warn("agent turn failed: {error}", error=type(exc).__name__)
        await emit({"type": "error", "code": "agent_failed", "text": type(exc).__name__, "said": state["said"]})
    finally:
        done.put_nowait(None)


async def _stream(agent, deps, prompt, messages, extra, emit, state, t0):  # noqa: ANN001, ANN202
    """Run the agent and speak its reply.

    A model response's text is held until the response ends: if it ends in a ZIGI or web call, that text was the
    model thinking out loud before checking (seen 09 Nov: "Looks like that got cut off..." then, after the call was
    refused, "I heard seven digits..."), so it is dropped and only the tool's lead-in is said. Only the final answer
    is spoken.
    """
    held = ""
    # Enforced once per turn (a retry is allowed through), only when ZIGI is free to answer, and not when ZIGI's
    # answer is already in the conversation (a repeat, or a reply the caller spoke over).
    needs_zigi = (
        not state.get("retried")
        and prompt is not None
        and asks_about_mtn(prompt)
        and deps.zigi.waiting_for == "none"
        and not state.get("note")
    )

    async def say(sentence: str, *, filler: bool = False) -> None:
        sentence = sentence.strip()
        if not sentence:
            return
        if _SKIP.match(sentence):
            state["skipped"] = True  # not said to us: stay quiet (side conversations, 09 Nov)
            return
        if not filler and needs_zigi and not deps.used_zigi and not deps.cache_hits and not deps.answer_known and not state["said"]:
            # Seen 09 Nov: "Can you tell me what MoMo is?" was answered from the model's own knowledge.
            logfire.warn("agent answered an MTN question without ZIGI; asking ZIGI instead")
            raise _Ungrounded()
        if not filler and (missing := ungrounded_numbers(sentence, deps.grounding)):
            logfire.warn("dropped an agent sentence with ungrounded numbers {missing}", missing=missing)
            if not deps.used_zigi:
                raise _Ungrounded()
            return
        if state["first"] is None:
            state["first"] = round((time.monotonic() - t0) * 1000)
        state["said"] += 1
        await emit({"type": "say", "text": sentence, **({"filler": True} if filler else {})})

    async def lead(text: str) -> None:
        if state["said"]:
            return
        line = text if 0 < len(text.split()) <= 14 and not ungrounded_numbers(text, deps.grounding) else lead_in(state["last_lead"])
        state["last_lead"] = line
        await say(line, filler=True)

    deps.lead = lead
    # Mid-step (Zigi waiting for a number, code or confirmation) is where the model drafted, called a tool, got
    # refused and drafted again. There, hold the whole reply. Otherwise stream sentences as they complete.
    stream_now = deps.zigi.waiting_for in {"none", "confirmation"}

    async def release() -> None:
        nonlocal held
        text, held = held, ""
        for sentence in _SENTENCE_END.split(text):
            await say(sentence)

    result = None
    instructions = "\n\n".join(extra) or None
    async with agent.run_stream_events(prompt, deps=deps, message_history=messages, instructions=instructions) as events:
        async for ev in events:
            if isinstance(ev, PartStartEvent) and isinstance(ev.part, TextPart):
                held += ("\n" if held else "") + ev.part.content
            elif isinstance(ev, PartDeltaEvent) and isinstance(ev.delta, TextPartDelta):
                held += ev.delta.content_delta
                if stream_now and _SENTENCE_END.search(held):
                    *complete, held = _SENTENCE_END.split(held)
                    for sentence in complete:
                        await say(sentence)
            elif isinstance(ev, PartStartEvent) and isinstance(ev.part, ToolCallPart):
                # Only the answer after the last tool call is spoken (text before "remember" was said twice, 09 Nov).
                if held.strip():
                    logfire.info("dropped agent text written before a {tool} call", tool=ev.part.tool_name)
                held = ""
            elif isinstance(ev, FunctionToolCallEvent):
                await emit({"type": "tool", "name": ev.part.tool_name, "phase": "start"})
            elif isinstance(ev, FunctionToolResultEvent):
                await emit({"type": "tool", "name": ev.part.tool_name, "phase": "end"})
            elif isinstance(ev, AgentRunResultEvent):
                result = ev.result
    await release()
    assert result is not None
    return result
