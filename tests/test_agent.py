import json
import time

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.sunco import Message
from zigi_agent.rules import (
    classify_waiting,
    host_allowed,
    is_affirmative,
    links_in,
    page_text,
    shape_input,
    spoken_digits,
    ungrounded_numbers,
)

from .conftest import mock_settings


def msg(text, actions=None):
    content = {"type": "text", "text": text}
    if actions:
        content["actions"] = actions
    return Message(id="m", conversation_id="c", author_type="business", received="", content=content)


# ------------------------------------------------------------------ guardrails (design 5.8.4)
@pytest.mark.parametrize(
    "said,digits",
    [
        ("my OTP is 4 5 1 2 9", "45129"),
        ("four five one two nine", "45129"),
        ("it's 45129", "45129"),
        ("double five, one, oh, nine", "55109"),
        ("the code is 12 34", "1234"),
        ("I want to buy data for my brother", ""),
    ],
)
def test_spoken_digits(said, digits):
    assert spoken_digits(said) == digits


def test_otp_is_reduced_to_digits_or_refused():
    assert shape_input("my OTP is four five one two nine", "otp", "") == ("45129", None)
    sent, refusal = shape_input("it's one two", "otp", "it's one two")
    assert sent is None and "digit" in refusal
    # No number at all: the user changed the subject, so it goes to Zigi as said.
    # Mid-step, anything else is answered by the agent itself (Zigi would only ask for the code again)...
    sent, refusal = shape_input("I don't have it", "otp", "I don't have it")
    assert sent is None and "cancel" in refusal
    # Zigi can't leave a step (probed 09 Nov), so "cancel" is never sent mid-step either.
    assert shape_input("cancel", "otp", "cancel")[0] is None


def test_phone_number_is_normalised():
    assert shape_input("233 24 123 4567", "phone_number", "")[0] == "0241234567"
    assert shape_input("zero two four one two three four five six seven", "phone_number", "")[0] == "0241234567"


def test_agent_cannot_confirm_for_the_user():
    assert shape_input("Yes", "confirmation", "how much is it again?")[1] is not None
    assert shape_input("Yes", "confirmation", "yeah go ahead") == ("Yes", None)
    assert not is_affirmative("no, don't do it")


def test_numbers_must_come_from_zigi_or_the_user():
    sources = ["You can buy a data bundle by dialling *138#. 5GB costs GH₵25."]
    assert ungrounded_numbers("Dial *138# and 5GB is 25 cedis.", sources) == []
    assert ungrounded_numbers("Dial *156# for 10GB.", sources) == ["*156#", "10"]


def test_numbered_instructions_are_not_a_request():
    """Seen 08 Nov: a SIM swap how-to ("3. Enter your phone number.") was taken as a request for one."""
    howto = "To perform a SIM swap:\n1. Buy a blank SIM.\n2. Go to the site.\n3. Enter your phone number.\n4. Select SIM unavailable."
    assert classify_waiting([msg(howto)]) == "none"
    assert classify_waiting([msg("Please enter your phone number in the format 024XXXXXXX")]) == "phone_number"


def test_page_text_prefers_main_content():
    html = (
        "<html><title>Dwenase | MTN</title><nav>Menu Personal Business</nav>"
        "<main><h1>Dwenase</h1><p>Open Monday - Friday 08:00 - 17:00</p></main><script>x()</script></html>"
    )
    title, text = page_text(html)
    assert title == "Dwenase | MTN" and "08:00" in text and "Menu" not in text and "x()" not in text


def test_only_allowed_https_hosts():
    assert host_allowed("https://mtn.com.gh/stores/", ["mtn.com.gh"])
    assert host_allowed("https://shop.mtn.com.gh/x", ["mtn.com.gh"])
    assert not host_allowed("http://mtn.com.gh/", ["mtn.com.gh"])
    assert not host_allowed("https://evilmtn.com.gh/", ["mtn.com.gh"])
    assert links_in("Visit [https://mtn.com.gh/stores/](https://mtn.com.gh/stores/).") == [
        "https://mtn.com.gh/stores/",
        "https://mtn.com.gh/stores/",
    ]


def test_waiting_labels():
    assert classify_waiting([msg("Please enter the OTP we sent to your number.")]) == "otp"
    assert classify_waiting([msg("Confirm purchase of 1GB for GH₵5? Reply YES or NO.")]) == "confirmation"
    assert classify_waiting([msg("Pick one", [{"type": "reply", "text": "A"}, {"type": "reply", "text": "B"}])]) == "menu_choice"
    assert classify_waiting([msg("MoMo is Mobile Money.")]) == "none"


# ------------------------------------------------------------------ the agent-mode session
@pytest.fixture
def agent_client():
    with TestClient(create_app(mock_settings(agent_mode_enabled=True, agent_quiet_s=0.2))) as c:
        yield c


def run_turn(ws, text, until, timeout=8.0):
    """Send typed text; collect events until `until(events)` is true."""
    ws.send_text(json.dumps({"type": "text", "text": text}))
    events = []
    end = time.time() + timeout
    while time.time() < end:
        m = ws.receive()
        if m.get("bytes"):
            events.append({"type": "_audio"})
        else:
            events.append(json.loads(m["text"]))
        if until(events):
            return events
    raise AssertionError(f"timed out; got {[e['type'] for e in events]}")


def said(events):
    return " ".join(e["text"] for e in events if e["type"] == "agent_text")


def sent_to_zigi(events):
    return [e["message"]["content"]["text"] for e in events if e["type"] == "user_message"]


def has_metrics(events):
    """The turn is over: metrics sent, and the gateway is back to listening after them."""
    seen = False
    for e in events:
        seen |= e["type"] == "metrics"
        if seen and e["type"] == "state" and e["state"] == "listening":
            return True
    return False


def test_config_offers_both_modes(agent_client):
    cfg = agent_client.get("/v1/config").json()
    assert cfg["voice_modes"] == ["relay", "agent"] and cfg["voice_mode_default"] == "relay"


def test_agent_mode_is_off_without_the_flag(client):
    """OPENROUTER_API_KEY is only a credential; AGENT_MODE_ENABLED turns the mode on."""
    assert client.get("/v1/config").json()["voice_modes"] == ["relay"]
    token = client.post("/v1/session", json={}).json()["token"]
    with client.websocket_connect(f"/v1/voice/session?token={token}&mode=agent") as ws:
        assert json.loads(ws.receive_text())["mode"] == "relay"


def test_small_talk_is_answered_without_zigi(agent_client):
    token = agent_client.post("/v1/session", json={}).json()["token"]
    with agent_client.websocket_connect(f"/v1/voice/session?token={token}&mode=agent") as ws:
        assert json.loads(ws.receive_text())["mode"] == "agent"
        ev = run_turn(ws, "Hi, how are you?", has_metrics)
    assert "good, thanks" in said(ev)
    assert sent_to_zigi(ev) == []
    m = next(e for e in ev if e["type"] == "metrics")
    assert m["mode"] == "agent" and m["route"] == "direct" and m["bot_ms"] == 0


def test_faq_goes_through_zigi_with_a_hold_line(agent_client):
    token = agent_client.post("/v1/session", json={}).json()["token"]
    with agent_client.websocket_connect(f"/v1/voice/session?token={token}&mode=agent") as ws:
        ws.receive_text()
        ev = run_turn(ws, "How does roaming work?", has_metrics)
    assert sent_to_zigi(ev) == ["How does roaming work?"]
    assert "*135#" in said(ev)
    assert any(e["type"] == "audio_start" and e["message_id"].startswith("hold-") for e in ev)
    m = next(e for e in ev if e["type"] == "metrics")
    assert m["route"] == "zigi" and m["first_audio_ms"] <= m["total_ms"]


def test_otp_in_a_sentence_reaches_zigi_as_digits(agent_client):
    token = agent_client.post("/v1/session", json={}).json()["token"]
    with agent_client.websocket_connect(f"/v1/voice/session?token={token}&mode=agent") as ws:
        ws.receive_text()
        ev = run_turn(ws, "I want to buy a data bundle", has_metrics)
        assert "OTP" in said(ev)
        ev = run_turn(ws, "my OTP is four five one two nine", has_metrics)
        assert sent_to_zigi(ev) == ["45129"]
        assert "Confirm" in said(ev)
        # The mock agent forwards anything; the gateway still refuses a yes the user did not give.
        ev = run_turn(ws, "Yes", has_metrics)
        assert sent_to_zigi(ev) == ["Yes"] and "Done" in said(ev)


def test_relay_mode_is_unchanged(agent_client):
    token = agent_client.post("/v1/session", json={}).json()["token"]
    with agent_client.websocket_connect(f"/v1/voice/session?token={token}") as ws:
        assert json.loads(ws.receive_text())["mode"] == "relay"
        ev = run_turn(ws, "Hi, how are you?", has_metrics)
    assert sent_to_zigi(ev) == ["Hi, how are you?"] and said(ev) == ""
    assert next(e for e in ev if e["type"] == "metrics")["route"] == "relay"


def test_speaking_again_while_zigi_works_never_sends_twice(agent_client):
    """Seen 08 Nov: a question and an OTP reached Zigi twice when the caller spoke again mid-turn."""
    token = agent_client.post("/v1/session", json={}).json()["token"]
    with agent_client.websocket_connect(f"/v1/voice/session?token={token}&mode=agent") as ws:
        ws.receive_text()
        ws.send_text(json.dumps({"type": "text", "text": "How does roaming work?"}))
        events = []
        while not any(e["type"] == "user_message" for e in events):  # Zigi is now working on it
            m = ws.receive()
            if m.get("text"):
                events.append(json.loads(m["text"]))
        events += run_turn(ws, "How does roaming work?", has_metrics)
    assert sent_to_zigi(events).count("How does roaming work?") == 1
    # The answer to the first ask was not spoken over the caller's newer words: only the latest turn speaks.
    first = next(e["turn_id"] for e in events if e["type"] == "transcript_final")
    assert not [e for e in events if e["type"] == "agent_text" and e["turn_id"] == first and not e.get("filler")]


def test_issues_from_the_09_nov_call():
    """Each line is something that went wrong on the 09 Nov fibre call."""
    from zigi_agent.rules import _ask

    # Zigi's request ended with a format line, so "waiting for a phone number" was missed.
    ask = _ask("Please enter your phone number in the format:\n024XXXXXXX or 23324XXXXXX")
    assert "phone number" in ask
    assert classify_waiting([msg("Please enter your phone number in the format:\n024XXXXXXX or 23324XXXXXX")]) == "phone_number"
    # Half a number, a "Yes", and "why was it rejected?" all reached Zigi while it waited for the number.
    assert shape_input("026-226-0-", "phone_number", "026 226 0")[0] is None
    assert shape_input("Yes", "phone_number", "yes")[0] is None
    assert shape_input("Could you explain why the checker rejected my number?", "phone_number", "why")[0] is None
    # After the check had finished, an old OTP was sent three more times.
    assert shape_input("606461", "none", "606461")[0] is None
    assert shape_input("2", "menu_choice", "2") == ("2", None)
    # The agent cancelled the check on its own when the caller only asked about something else.
    assert shape_input("cancel", "phone_number", "What can I buy on the MTN e-shop?")[0] is None
    assert shape_input("cancel", "phone_number", "forget it, never mind")[0] is None


def test_a_code_said_in_two_parts_reaches_zigi_once_whole():
    """Seen 09 Nov: a pause mid-number sent half of it to Zigi. The gateway now waits briefly for the rest."""
    app = create_app(mock_settings(agent_mode_enabled=True, agent_quiet_s=0.2, agent_dictation_wait_s=1.0))
    with TestClient(app) as c:
        token = c.post("/v1/session", json={}).json()["token"]
        with c.websocket_connect(f"/v1/voice/session?token={token}&mode=agent") as ws:
            ws.receive_text()
            run_turn(ws, "I want to buy a data bundle", has_metrics)
            ws.send_text(json.dumps({"type": "text", "text": "it's four five"}))
            ev = run_turn(ws, "one two nine", has_metrics)
    assert sent_to_zigi(ev) == ["45129"]


def test_mtn_questions_are_recognised():
    from zigi_agent.rules import asks_about_mtn

    assert asks_about_mtn("Can you tell me what MoMo is?")
    assert asks_about_mtn("how much is the 5GB bundle")
    assert not asks_about_mtn("Hi, good morning")
    assert not asks_about_mtn("What can you do?")
    assert not asks_about_mtn("Sorry, can you say the MoMo part again?")
    assert not asks_about_mtn("Thanks, that's all")


def test_zigis_numbered_bundle_menu_is_answered_with_a_number():
    """Seen 09 Nov: the menu and "Please select ... corresponding number" came as two messages, so the menu was missed
    and sentences were sent instead of "4", six times over."""
    from zigi_agent.rules import ZigiState, pick_option

    menu = msg(
        "Available bundles are:\n\n1. 20.73MB @ GHS 0.5\n2. 41.46MB @ GHS 1\n3. 406.89MB @ GHS 3\n4. 837.55MB @ GHS 10\n7. Flexi Data"
    )
    ask = msg(
        "Please select the price of bundles you'd like to buy by providing the corresponding number "  # noqa: ISC003
        + '(e.g., "1" or "2") or type exit to stop the process.'
    )
    z = ZigiState()
    z.update([menu, ask])
    assert z.waiting_for == "menu_choice"
    assert [o["number"] for o in z.options] == ["1", "2", "3", "4", "7"]
    for said, number in [
        ("4", "4"),
        ("four", "4"),
        ("The 10 cedis is fine", "4"),
        ("I want the 837.55MB bundle for GHS 10", "4"),
        ("Flexi", "7"),
    ]:
        assert pick_option(z.options, said)["number"] == number, said
    for said in ["Yes", "What are all the available data bundles?"]:
        assert pick_option(z.options, said) is None, said


def test_how_to_steps_are_not_a_menu_and_offers_are_yes_no():
    """Seen 09 Nov: Zigi's numbered Just4U how-to was taken for a menu, and every question was refused after it."""
    from zigi_agent.rules import ZigiState

    how_to = msg(
        "You can buy a Just4U offer using the following methods:\n\nOption 1: MyMTN App\n1. Launch the MyMTN App.\n"
        '2. Select "Just4U" on the homepage.\n3. Choose your preferred option.\n\nOption 2: USSD\n1. Dial *141#.\n'
        "2. Select your preferred option.\n\nWould you like me to help you purchase a Just4U bundle?"
    )
    z = ZigiState()
    z.update([how_to])
    assert z.waiting_for == "confirmation" and z.options == []


def test_echo_of_the_assistant_is_dropped():
    from app.session import VoiceSession

    s = VoiceSession.__new__(VoiceSession)
    s.recent_spoken = [(time.monotonic(), "Got it. What else can I help with?")]
    assert s._is_echo("What else can I help with?")
    assert not s._is_echo("How do I buy airtime?")
    assert not s._is_echo("yes")
