import json
import struct
import time


def auth(token):
    return {"authorization": f"Bearer {token}"}


def wait_for_bot(client, token, n=1, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        msgs = client.get("/v1/conversations/me/messages", headers=auth(token)).json()["messages"]
        bots = [m for m in msgs if m["author_type"] == "business"]
        if len(bots) >= n:
            return bots
        time.sleep(0.05)
    raise AssertionError("bot did not reply")


def test_requires_token(client):
    assert client.get("/v1/conversations/me/messages").status_code == 401
    assert client.get("/v1/conversations/me/messages", headers=auth("bad.1.x")).status_code == 401


def test_same_user_id_keeps_conversation(client):
    a = client.post("/v1/session", json={"user_id": "demo_user_1"}).json()
    b = client.post("/v1/session", json={"user_id": "demo_user_1"}).json()
    ca = client.get("/v1/conversations/me/messages", headers=auth(a["token"])).json()["conversation_id"]
    cb = client.get("/v1/conversations/me/messages", headers=auth(b["token"])).json()["conversation_id"]
    assert ca == cb


def test_text_round_trip_and_read_aloud(client, token):
    r = client.post("/v1/messages", json={"text": "How does roaming work?"}, headers=auth(token))
    assert r.status_code == 200
    bot = wait_for_bot(client, token)[0]
    assert "Roaming" in bot["content"]["text"]
    audio = client.get(f"/v1/messages/{bot['id']}/audio", params={"token": token})
    assert audio.status_code == 200 and audio.headers["x-cache"] == "miss" and audio.content[:4] == b"RIFF"
    again = client.get(f"/v1/messages/{bot['id']}/audio", params={"token": token})
    assert again.headers["x-cache"] == "hit" and again.content == audio.content


def test_read_aloud_rejects_other_users_message(client, token):
    client.post("/v1/messages", json={"text": "momo?"}, headers=auth(token))
    bot = wait_for_bot(client, token)[0]
    other = client.post("/v1/session", json={}).json()["token"]
    assert client.get(f"/v1/messages/{bot['id']}/audio", params={"token": other}).status_code == 404


def test_voice_note_transcribes(client, token):
    wav = b"RIFF" + b"\0" * 4000
    r = client.post("/v1/voice-notes", files={"file": ("n.wav", wav, "audio/wav")}, headers=auth(token))
    assert r.status_code == 200 and r.json()["transcript"]


def test_webhook_requires_secret(client):
    assert client.post("/webhooks/sunco", json={"events": []}).status_code == 401
    r = client.post("/webhooks/sunco", json={"events": []}, headers={"x-api-key": "hook-secret"})
    assert r.json() == {"published": 0}


def test_voice_turn_end_to_end(client, token):
    """1 s of speech -> mock STT final -> SunCo -> bot replies -> streamed PCM + metrics."""
    pcm = struct.pack("<320h", *([1000] * 320))
    seen = {"bot": 0, "audio_bytes": 0}
    events = []
    with client.websocket_connect(f"/v1/voice/session?token={token}") as ws:
        assert json.loads(ws.receive_text())["type"] == "ready"
        ws.send_text(json.dumps({"type": "speech_start"}))
        for _ in range(55):
            ws.send_bytes(pcm)
        ws.send_text(json.dumps({"type": "speech_end", "hangover_ms": 500}))
        deadline = time.time() + 8
        while time.time() < deadline:
            msg = ws.receive()
            if msg.get("bytes"):
                seen["audio_bytes"] += len(msg["bytes"])
                continue
            ev = json.loads(msg["text"])
            events.append(ev["type"])
            if ev["type"] == "bot_message":
                seen["bot"] += 1
            if ev["type"] == "metrics":
                metrics = ev
            if ev["type"] == "audio_end" and seen["bot"] >= 2:
                break
    assert "transcript_final" in events and "user_message" in events
    assert seen["bot"] >= 2 and seen["audio_bytes"] > 0 and seen["audio_bytes"] % 2 == 0
    assert metrics["bot_ms"] >= 0 and metrics["total_ms"] >= metrics["bot_ms"]


def test_voice_rejects_bad_token(client):
    import pytest
    from starlette.websockets import WebSocketDisconnect

    with pytest.raises(WebSocketDisconnect) as exc, client.websocket_connect("/v1/voice/session?token=nope") as ws:
        ws.receive_text()
    assert exc.value.code == 4401
