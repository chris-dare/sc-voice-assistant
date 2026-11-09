import asyncio
import re

from app.bus import MemoryBus, channel_for
from app.delivery import publish_bot_message
from app.sunco import Message
from zigi_agent.agent import CallDeps, _collect_burst
from zigi_agent.config import AgentSettings
from zigi_agent.rules import ZigiState


class NoPoll:
    async def list_messages(self, conversation_id):
        return []


def bot(text, received):
    return Message(id=text[:8], conversation_id="c", author_type="business", received=received, content={"type": "text", "text": text})


def test_the_answer_after_a_quiet_gap_still_arrives_on_the_channel():
    """Seen 09 Nov: timing out on the subscription closed it, so Zigi's answer after its greeting never arrived."""

    async def go():
        bus = MemoryBus()
        settings = AgentSettings(_env_file=None, zigi_quiet_s=0.4, zigi_timeout_s=6)

        async def emit(_):
            return None

        deps = CallDeps(
            provider=NoPoll(),
            settings=settings,
            conversation_id="c",
            sunco_user_id="u",
            user_said="What is MoMo?",
            zigi=ZigiState(),
            grounding=[],
            skip=[],
            preamble=[re.compile(p, re.I) for p in settings.zigi_preamble_patterns],
            emit=emit,
        )
        posted = Message(
            id="p",
            conversation_id="c",
            author_type="user",
            received="2026-11-09T08:00:00Z",
            content={"type": "text", "text": "What is MoMo?"},
        )
        async with bus.subscribe(channel_for("c")) as sub:

            async def zigi():
                await asyncio.sleep(0.1)
                await publish_bot_message(
                    bus, bot("Good morning! My name is Zigi, your friendly MTN Ghana assistant.", "2026-11-09T08:00:02Z")
                )
                await asyncio.sleep(1.5)  # longer than the quiet window: the old code had stopped listening by now
                await publish_bot_message(bus, bot("MoMo is MTN's mobile money service.", "2026-11-09T08:00:15Z"))

            task = asyncio.create_task(zigi())
            burst = await _collect_burst(deps, posted, sub)
            await task
        return [m.text for m in burst]

    texts = asyncio.run(go())
    assert texts[-1] == "MoMo is MTN's mobile money service."
