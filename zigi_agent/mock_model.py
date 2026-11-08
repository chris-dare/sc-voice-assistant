"""A scripted model for tests and MOCK_PROVIDERS demos: small talk answered directly, everything else via ZIGI."""

from __future__ import annotations

import json
import re
from collections.abc import AsyncIterator

from pydantic_ai.messages import ModelMessage, ModelRequest, RetryPromptPart, ToolReturnPart, UserPromptPart
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, DeltaToolCalls, FunctionModel

_SMALL_TALK = re.compile(
    r"^\s*(hi|hello|hey|good (morning|afternoon|evening)|how are you|thanks|thank you|who are you|what can you do)\b", re.I
)


def _words(text: str) -> list[str]:
    return re.findall(r"\S+\s*", text)


async def _stream(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[str | DeltaToolCalls]:
    last = messages[-1]
    parts = last.parts if isinstance(last, ModelRequest) else []
    tool_return = next((p for p in parts if isinstance(p, ToolReturnPart)), None)
    retry = next((p for p in parts if isinstance(p, RetryPromptPart)), None)
    user = next((p for p in parts if isinstance(p, UserPromptPart)), None)
    if retry is not None:
        for w in _words("Sorry, could you say that again?"):
            yield w
        return
    if tool_return is not None:
        data = json.loads(str(tool_return.content)) if str(tool_return.content).startswith("{") else {}
        texts = [m["text"] for m in data.get("zigi_messages", []) if m.get("text")]
        options = next((m["options"] for m in data.get("zigi_messages", []) if m.get("options")), [])
        reply = " ".join(texts) or "I couldn't get an answer on that."
        if options:
            reply += " You can pick " + ", ".join(options[:-1]) + (" or " if len(options) > 1 else "") + options[-1] + "."
        for w in _words(reply):
            yield w
        return
    said = str(user.content) if user is not None else ""
    if user is None:  # a push from Zigi: say what it said
        for w in _words("Okay, I've got your photo. Thanks!"):
            yield w
        return
    if _SMALL_TALK.search(said):
        for w in _words("I'm good, thanks for asking! What can I sort out for you?"):
            yield w
        return
    yield {0: DeltaToolCall(name="ask_zigi", json_args=json.dumps({"text": said}), tool_call_id="call_mock")}


def mock_model() -> FunctionModel:
    return FunctionModel(stream_function=_stream, model_name="mock-agent")
