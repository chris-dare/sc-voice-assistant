import json

from fastapi.testclient import TestClient

from zigi_agent.config import AgentSettings
from zigi_agent.service import create_app


def service():
    return TestClient(
        create_app(AgentSettings(_env_file=None, mock_providers=True, agent_service_token="t", zigi_poll_interval_s=0.05, zigi_quiet_s=0.2))
    )


def turn(client, text, **extra):
    body = {"conversation_id": "c1", "sunco_user_id": "u1", "text": text, **extra}
    r = client.post("/v1/turn", json=body, headers={"Authorization": "Bearer t"})
    assert r.status_code == 200
    return [json.loads(line) for line in r.text.splitlines() if line.strip()]


def test_needs_the_service_token():
    with service() as c:
        assert c.post("/v1/turn", json={"conversation_id": "c", "sunco_user_id": "u", "text": "hi"}).status_code == 401


def test_small_talk_streams_sentences_and_state():
    with service() as c:
        events = turn(c, "Hi there")
    says = [e["text"] for e in events if e["type"] == "say"]
    done = events[-1]
    assert says and done["type"] == "done" and done["route"] == "direct" and done["history"]


def test_zigi_call_has_a_lead_in_and_returns_zigi_state():
    with service() as c:
        events = turn(c, "I want to buy a data bundle")
        done = events[-1]
        assert any(e["type"] == "say" and e.get("filler") for e in events)
        assert [e["message"]["content"]["text"] for e in events if e["type"] == "zigi_posted"] == ["I want to buy a data bundle"]
        assert done["route"] == "zigi" and done["zigi_state"]["waiting_for"] == "otp"
        # Next turn carries the state back: the OTP said in a sentence reaches Zigi as digits.
        events = turn(
            c, "my OTP is four five one two nine", history=done["history"], zigi_state=done["zigi_state"], grounding=done["grounding"]
        )
        assert [e["message"]["content"]["text"] for e in events if e["type"] == "zigi_posted"] == ["45129"]
        done = events[-1]
        # A "yes" the caller didn't give is refused by the guardrail; nothing reaches Zigi.
        events = turn(c, "hmm let me think", history=done["history"], zigi_state=done["zigi_state"], grounding=done["grounding"])
        assert not [e for e in events if e["type"] == "zigi_posted" and e["message"]["content"]["text"].lower() == "yes"]


def test_the_same_message_is_not_sent_to_zigi_twice():
    with service() as c:
        events = turn(c, "How does roaming work?")
        done = events[-1]
        again = turn(c, "How does roaming work?", history=done["history"], zigi_state=done["zigi_state"], sends=done["sends"])
    assert [e for e in again if e["type"] == "zigi_posted"] == []


def test_a_superseded_turn_never_reaches_zigi():
    """10 Nov: the gateway cancelled a turn and started a newer one, but the old request kept running on the agent
    service and both reached Zigi. A turn older than the call's latest turn number stops before sending."""
    import asyncio

    with service() as c:
        store = c.app.state.engine.store
        asyncio.run(store.set_json("turnseq:c1", 5, 60))
        stale = turn(c, "How does roaming work?", turn_seq=4)
        assert [e for e in stale if e["type"] == "zigi_posted"] == []
        assert not [e for e in stale if e["type"] == "error"]
        current = turn(c, "How does roaming work?", turn_seq=5)
        assert [e for e in current if e["type"] == "zigi_posted"]
