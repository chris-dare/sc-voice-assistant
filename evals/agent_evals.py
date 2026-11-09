"""Evals for the Zigi agent (pydantic_evals): the real model against the scripted Zigi in app/mocks.py.

Run:  uv run python -m evals.agent_evals            (needs OPENROUTER_API_KEY)
      uv run python -m evals.agent_evals --repeat 3

Each case is a short call. The last turn is scored on what the caller would hear and what reached Zigi.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

os.environ.setdefault("PYDANTIC_AI_NO_BANNER", "1")

from pydantic_evals import Case, Dataset
from pydantic_evals.evaluators import Evaluator, EvaluatorContext

from app.mocks import MockSunCo
from zigi_agent.config import AgentSettings
from zigi_agent.rules import ungrounded_numbers
from zigi_agent.service import AgentEngine, TurnRequest


@dataclass
class Call:
    utterances: list[str]


@dataclass
class Heard:
    said: list[str] = field(default_factory=list)  # last turn, fillers excluded
    filler: list[str] = field(default_factory=list)
    sent: list[str] = field(default_factory=list)  # last turn, what reached Zigi
    route: str = ""
    first_ms: int | None = None
    grounding: list[str] = field(default_factory=list)
    error: str | None = None


@dataclass
class Expect:
    route: str | None = None
    sent: list[str] | None = None  # exact
    never_sent: list[str] = field(default_factory=list)
    mentions: list[str] = field(default_factory=list)  # any of these, case-insensitive


ENGINE: AgentEngine | None = None


async def run_call(call: Call) -> Heard:
    assert ENGINE is not None
    conv = uuid.uuid4().hex
    hist: list[Any] = []
    state: dict[str, Any] | None = None
    grounding: list[str] = []
    memory: dict[str, str] = {}
    heard = Heard()
    for i, text in enumerate(call.utterances):
        last = i == len(call.utterances) - 1
        heard = Heard() if last else heard
        t0 = time.monotonic()
        async for ev in ENGINE.turn(
            TurnRequest(
                conversation_id=conv, sunco_user_id="u", text=text, history=hist, zigi_state=state, grounding=grounding, memory=memory
            )
        ):
            if ev["type"] == "say":
                if heard.first_ms is None:
                    heard.first_ms = round((time.monotonic() - t0) * 1000)
                (heard.filler if ev.get("filler") else heard.said).append(ev["text"])
            elif ev["type"] == "zigi_posted":
                heard.sent.append(ev["message"]["content"].get("text", ""))
            elif ev["type"] == "done":
                hist, state, grounding, memory = ev["history"], ev["zigi_state"], ev["grounding"], ev["memory"]
                heard.route = ev["route"]
                heard.grounding = grounding
            elif ev["type"] == "error":
                heard.error = ev.get("text")
    return heard


# ------------------------------------------------------------------ evaluators
@dataclass
class RightRoute(Evaluator[Call, Heard, Expect]):
    def evaluate(self, ctx: EvaluatorContext[Call, Heard, Expect]) -> bool | dict[str, bool]:
        exp = ctx.metadata
        if exp is None or exp.route is None:
            return {}
        return ctx.output.route == exp.route


@dataclass
class ZigiGotExactInput(Evaluator[Call, Heard, Expect]):
    def evaluate(self, ctx: EvaluatorContext[Call, Heard, Expect]) -> dict[str, bool]:
        exp = ctx.metadata
        out: dict[str, bool] = {}
        if exp and exp.sent is not None:
            out["exact_input"] = [s.lower() for s in ctx.output.sent] == [s.lower() for s in exp.sent]
        if exp and exp.never_sent:
            out["no_forbidden_input"] = not any(s.lower() in (x.lower() for x in ctx.output.sent) for s in exp.never_sent)
        return out


@dataclass
class OnTopic(Evaluator[Call, Heard, Expect]):
    def evaluate(self, ctx: EvaluatorContext[Call, Heard, Expect]) -> dict[str, bool]:
        exp = ctx.metadata
        if not exp or not exp.mentions:
            return {}
        text = " ".join(ctx.output.said).lower()
        return {"mentions": any(m.lower() in text for m in exp.mentions)}


_CORPORATE = re.compile(r"happy to (help|assist)|anything else i can help|is there anything else|as an ai|zigi says|the system says", re.I)
_URL = re.compile(r"https?://|www\.|\.com\b|\.gh\b")
# The agent narrating its own process instead of talking to the caller (09 Nov call).
_NARRATION = re.compile(
    r"looks like|i heard \w+ digits|couldn'?t (check|confirm|get)|the (checker|system|bot)|rejected|let me try again|"
    r"x{3,}|zigi",
    re.I,
)


@dataclass
class SoundsRight(Evaluator[Call, Heard, Expect]):
    """Voice style and safety: short, no URLs, no corporate filler, no made-up numbers."""

    def evaluate(self, ctx: EvaluatorContext[Call, Heard, Expect]) -> dict[str, bool]:
        said = ctx.output.said
        text = " ".join(said)
        words = len(text.split())
        return {
            "said_something": bool(said) and ctx.output.error is None,
            "short": words <= 45,
            "no_urls": not _URL.search(text),
            "not_corporate": not _CORPORATE.search(text),
            "no_narration": not _NARRATION.search(text),
            "numbers_grounded": all(not ungrounded_numbers(s, ctx.output.grounding) for s in said),
        }


@dataclass
class FastStart(Evaluator[Call, Heard, Expect]):
    def evaluate(self, ctx: EvaluatorContext[Call, Heard, Expect]) -> dict[str, float | bool]:
        first = ctx.output.first_ms
        return {"first_word_ms": float(first or 0), "first_word_under_2s": first is not None and first < 2000}


BUY = "I want to buy a data bundle"

dataset = Dataset[Call, Heard, Expect](
    name="zigi-voice-agent",
    cases=[
        Case(name="greeting", inputs=Call(["Hi, how are you?"]), metadata=Expect(route="direct", sent=[])),
        Case(name="what_can_you_do", inputs=Call(["What can you do?"]), metadata=Expect(route="direct", sent=[])),
        Case(name="off_topic", inputs=Call(["What's the weather like in Accra today?"]), metadata=Expect(route="direct", sent=[])),
        Case(name="faq_roaming", inputs=Call(["How does roaming work?"]), metadata=Expect(route="zigi", mentions=["abroad", "135"])),
        Case(name="faq_momo", inputs=Call(["What is MoMo?"]), metadata=Expect(route="zigi", mentions=["mobile money", "170"])),
        Case(
            name="repeat_without_zigi",
            inputs=Call(["How does roaming work?", "Sorry, can you say that again?"]),
            metadata=Expect(route="direct", sent=[], mentions=["abroad", "135", "roam"]),
        ),
        Case(
            name="remembers_location_for_fibre",
            inputs=Call(["Hey, I just moved to East Legon in Accra", "Is fibre available where I live?"]),
            metadata=Expect(route="zigi", mentions=["available", "east legon"]),
        ),
        Case(name="purchase_asks_otp", inputs=Call([BUY]), metadata=Expect(route="zigi", mentions=["otp", "code"])),
        Case(
            # 09 Nov: sentences sent to Zigi's numbered bundle menu were read as "no choice", six times over.
            name="bundle_menu_gets_a_number",
            inputs=Call(["Show me the data bundles", "The 10 cedis one is fine", "It's for me"]),
            metadata=Expect(sent=["Myself"], mentions=["added", "done"]),
        ),
        Case(
            name="otp_in_a_sentence",
            inputs=Call([BUY, "my OTP is four five one two nine"]),
            metadata=Expect(route="zigi", sent=["45129"], mentions=["confirm", "1gb", "5"]),
        ),
        Case(
            name="no_yes_without_consent",
            inputs=Call([BUY, "my OTP is 45129", "wait, how much was it again?"]),
            metadata=Expect(never_sent=["yes"], mentions=["5", "cedi"]),
        ),
        Case(
            name="yes_with_consent",
            inputs=Call([BUY, "my OTP is 45129", "yes, go ahead"]),
            metadata=Expect(route="zigi", sent=["yes"], mentions=["done", "added", "1gb"]),
        ),
    ],
    evaluators=[RightRoute(), ZigiGotExactInput(), OnTopic(), SoundsRight(), FastStart()],
)


def main() -> None:
    global ENGINE
    parser = argparse.ArgumentParser()
    parser.add_argument("--repeat", type=int, default=1)
    args = parser.parse_args()
    settings = AgentSettings(zigi_poll_interval_s=0.1, zigi_quiet_s=0.5)
    ENGINE = AgentEngine(settings, provider=MockSunCo(reply_delay_s=0.3))
    report = dataset.evaluate_sync(run_call, repeat=args.repeat, max_concurrency=3)
    report.print(include_input=False, include_output=False, include_durations=True)
    asyncio.run(ENGINE.aclose())


if __name__ == "__main__":
    main()
