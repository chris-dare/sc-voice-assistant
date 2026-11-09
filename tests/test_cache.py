import asyncio

from app.store import MemoryStore
from tests.test_agent_service import service, turn
from zigi_agent.cache import SemanticCache, answer_cacheable, question_cacheable


def test_only_general_questions_are_cacheable():
    assert question_cacheable("How does roaming work abroad?", "none")
    assert not question_cacheable("How does roaming work abroad?", "otp")
    assert not question_cacheable("what is my balance", "none")
    assert not question_cacheable("I want to buy a data bundle", "none")
    assert not question_cacheable("is 0244123456 registered", "none")
    assert not question_cacheable("hi", "none")


def test_only_plain_answers_are_cacheable():
    ok = {"zigi_messages": [{"text": "Roaming lets you use your MTN number abroad. Dial *135# to check rates."}], "waiting_for": "none"}
    assert answer_cacheable(ok)
    # Zigi's usual closing question is not a request (seen 09 Nov: every real answer was skipped because of it).
    closing = {
        "zigi_messages": [
            {
                "text": "MoMo is MTN's mobile money service. You can send and receive money and pay bills. "
                "Is there anything else I can assist you with?"
            }
        ],
        "waiting_for": "none",
    }
    assert answer_cacheable(closing)
    assert not answer_cacheable({**ok, "waiting_for": "otp"})
    assert not answer_cacheable({**ok, "timed_out": True})
    assert not answer_cacheable(
        {"zigi_messages": [{"text": "Hi KOFI, please enter your number to continue with roaming."}], "waiting_for": "none"}
    )


def test_a_repeat_question_from_another_caller_skips_zigi():
    with service() as c:
        first = turn(c, "How does roaming work?")
        assert first[-1]["route"] == "zigi"
        second = turn(c, "how does roaming work", conversation_id="c2", sunco_user_id="u2")
    assert [e for e in second if e["type"] == "zigi_posted"] == []
    assert second[-1]["route"] == "cache"
    assert "Roaming" in " ".join(e["text"] for e in second if e["type"] == "say")


def test_personal_questions_always_reach_zigi():
    with service() as c:
        turn(c, "I want to buy a data bundle")
        again = turn(c, "I want to buy a data bundle", conversation_id="c2", sunco_user_id="u2")
    assert [e for e in again if e["type"] == "zigi_posted"]


class FakeEmbedder:
    async def embed(self, text):
        return [1.0, 0.0] if "roam" in text.lower() else [0.0, 1.0]


def test_semantic_match_uses_the_embedding_threshold():
    async def go():
        cache = SemanticCache(MemoryStore(), FakeEmbedder(), ttl_s=60, threshold=0.9)
        result = {"zigi_messages": [{"text": "Roaming lets you use your number abroad, on by default."}], "waiting_for": "none"}
        await cache.store_answer("How does roaming work?", result, None)
        hit, _ = await cache.lookup("Can I use my line when roaming overseas?")
        miss, _ = await cache.lookup("What are MoMo charges?")
        return hit, miss

    hit, miss = asyncio.run(go())
    assert hit is not None and hit.similarity > 0.99 and miss is None
