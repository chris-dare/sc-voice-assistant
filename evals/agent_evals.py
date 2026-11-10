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
    refusals: int = 0  # guardrail refusals over the whole call: the over-blocking measure
    task: dict[str, Any] | None = None  # the structured task record after the call


@dataclass
class Expect:
    route: str | None = None
    sent: list[str] | None = None  # exact
    never_sent: list[str] = field(default_factory=list)
    mentions: list[str] = field(default_factory=list)  # any of these, case-insensitive
    silent: bool = False  # the last thing heard wasn't said to the agent: it should say nothing
    task: dict[str, str] | None = None  # expected fields of the task record at the end, e.g. {"kind": "puk"}


ENGINE: AgentEngine | None = None


async def run_call(call: Call) -> Heard:
    assert ENGINE is not None
    conv = uuid.uuid4().hex
    hist: list[Any] = []
    state: dict[str, Any] | None = None
    grounding: list[str] = []
    memory: dict[str, str] = {}
    heard = Heard()
    refusals = 0
    last_reply = ""
    task: dict[str, Any] | None = None
    for i, text in enumerate(call.utterances):
        last = i == len(call.utterances) - 1
        heard = Heard() if last else heard
        t0 = time.monotonic()
        async for ev in ENGINE.turn(
            TurnRequest(
                conversation_id=conv,
                sunco_user_id="u",
                text=text,
                history=hist,
                zigi_state=state,
                grounding=grounding,
                memory=memory,
                last_reply=last_reply,
                task=task,
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
                refusals += int(ev.get("refusals") or 0)
                last_reply = " ".join(heard.said) or last_reply
                task = ev.get("task")
            elif ev["type"] == "error":
                heard.error = ev.get("text")
    heard.refusals = refusals
    heard.task = task
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
        if ctx.metadata and ctx.metadata.silent:
            return {"stayed_silent": not said and ctx.output.error is None}
        return {
            "said_something": bool(said) and ctx.output.error is None,
            "short": words <= 45,
            "no_urls": not _URL.search(text),
            "not_corporate": not _CORPORATE.search(text),
            "no_narration": not _NARRATION.search(text),
            "numbers_grounded": all(not ungrounded_numbers(s, ctx.output.grounding) for s in said),
        }


@dataclass
class TaskRecorded(Evaluator[Call, Heard, Expect]):
    """The structured task record (tasks.py) ends up as expected: kind, status and key fields."""

    def evaluate(self, ctx: EvaluatorContext[Call, Heard, Expect]) -> dict[str, bool]:
        exp = ctx.metadata
        if not exp or not exp.task:
            return {}
        got = ctx.output.task or {}
        return {"task": all(v.lower() in str(got.get(k, "")).lower() for k, v in exp.task.items())}


@dataclass
class NotOverBlocked(Evaluator[Call, Heard, Expect]):
    """Guardrails should rarely need to refuse: each refusal is a wasted model round trip, and many mean the rules
    are fighting the model (09 Nov: about 15 in three minutes). At most one per call."""

    def evaluate(self, ctx: EvaluatorContext[Call, Heard, Expect]) -> dict[str, float | bool]:
        return {"refusals": float(ctx.output.refusals), "at_most_one_refusal": ctx.output.refusals <= 1}


@dataclass
class FastStart(Evaluator[Call, Heard, Expect]):
    def evaluate(self, ctx: EvaluatorContext[Call, Heard, Expect]) -> dict[str, float | bool]:
        first = ctx.output.first_ms
        if ctx.metadata and ctx.metadata.silent:
            return {}
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
        Case(
            # 09 Nov: a side conversation in the room was answered. These are said to someone else.
            name="ignores_side_conversation",
            inputs=Call(["How does roaming work?", "Ama, did you lock the front door before we left?"]),
            metadata=Expect(route="ignored", sent=[], silent=True),
        ),
        Case(
            name="ignores_half_sentence_between_others",
            inputs=Call(["Hi", "no no, I told him to bring it tomorrow, he said"]),
            metadata=Expect(route="ignored", sent=[], silent=True),
        ),
        Case(
            # ...but an aside that is for the agent is still answered.
            name="answers_an_aside_meant_for_it",
            inputs=Call(["How does roaming work?", "sorry, my friend was talking. Can you say that again?"]),
            metadata=Expect(route="direct", sent=[], mentions=["abroad", "135", "roam"]),
        ),
        Case(
            # 09 Nov: a numbered how-to was taken for a menu, and every later question was refused.
            name="how_to_steps_are_not_a_menu",
            inputs=Call(["How do I buy a Just4U offer?", "Okay. And how does roaming work?"]),
            metadata=Expect(route="zigi", mentions=["abroad", "135", "roam"]),
        ),
        # Generalisation: situations no rule was written for. The agent should cope without fighting the guardrails.
        Case(
            name="question_in_the_middle_of_a_step",
            inputs=Call([BUY, "wait, how much does it cost again?"]),
            metadata=Expect(sent=[], mentions=["5", "cedi"]),
        ),
        Case(
            name="change_of_mind_mid_step",
            inputs=Call([BUY, "actually forget the bundle. How does roaming work?"]),
            metadata=Expect(mentions=["roam", "abroad", "135", "code", "otp", "finish", "first"]),
        ),
        Case(
            name="unseen_flow_puk",
            inputs=Call(["I need my PUK, my SIM is locked", "it's 0244123456"]),
            metadata=Expect(route="zigi", sent=["0244123456"], mentions=["puk", "1234"], task={"kind": "puk", "status": "done"}),
        ),
        Case(
            # 10 Nov: a choice said as an ordinal, then "for me", must reach Zigi as "1" and "Myself".
            name="ordinal_choice",
            inputs=Call(["Show me the data bundles", "The first one please", "It's for me"]),
            metadata=Expect(mentions=["added", "done"], task={"kind": "bundle_purchase", "status": "done", "recipient": "self"}),
        ),
        Case(
            # 10 Nov: read-back then yes. "Which is the cheapest?" -> offered one -> "Yes" is a choice.
            name="yes_after_an_offer_is_a_choice",
            inputs=Call(["Show me the data bundles", "Which one is the cheapest?", "Yes, that one", "For me"]),
            metadata=Expect(sent=["Myself"], mentions=["added", "done"]),
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
    evaluators=[RightRoute(), ZigiGotExactInput(), OnTopic(), SoundsRight(), NotOverBlocked(), TaskRecorded(), FastStart()],
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
