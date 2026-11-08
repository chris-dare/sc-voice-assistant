import asyncio
import json
import shutil
import socket
import subprocess
import time

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.store import LimitExceeded, Limits, MemoryStore, RedisStore

from .conftest import mock_settings


def limits(store=None, **kw):
    base = dict(enabled=True, per_minute={"messages": 3}, max_calls=1, daily_voice_s=60)
    base.update(kw)
    return Limits(store or MemoryStore(), **base)


def test_rate_limit_per_user_per_minute():
    async def go():
        lim = limits()
        for _ in range(3):
            await lim.hit("u1", "messages")
        with pytest.raises(LimitExceeded):
            await lim.hit("u1", "messages")
        await lim.hit("u2", "messages")  # other users are unaffected

    asyncio.run(go())


def test_call_slots_and_daily_minutes():
    async def go():
        lim = limits()
        await lim.call_started("u1", "call-a")
        await lim.call_started("u1", "call-a")  # a reconnect of the same call keeps its slot
        with pytest.raises(LimitExceeded):
            await lim.call_started("u1", "call-b")
        await lim.call_ended("u1", "call-a")
        await lim.call_started("u1", "call-b")
        with pytest.raises(LimitExceeded):
            await lim.heartbeat("u1", "call-b", 61)  # over the 60 s budget
        await lim.call_ended("u1", "call-b")
        with pytest.raises(LimitExceeded):
            await lim.call_started("u1", "call-c")

    asyncio.run(go())


def test_rest_limit_returns_429():
    app = create_app(mock_settings(limit_per_minute={"messages": 2}))
    with TestClient(app) as c:
        token = c.post("/v1/session", json={}).json()["token"]
        h = {"authorization": f"Bearer {token}"}
        codes = [c.post("/v1/messages", json={"text": "hi"}, headers=h).status_code for _ in range(3)]
    assert codes == [200, 200, 429]


def test_agent_memory_survives_a_reconnect():
    """Vercel ends a function at 300 s, so long calls reconnect: the agent must still know where the caller is."""
    app = create_app(mock_settings(agent_mode_enabled=True, agent_quiet_s=0.2))
    with TestClient(app) as c:
        token = c.post("/v1/session", json={}).json()["token"]
        url = f"/v1/voice/session?token={token}&mode=agent&call=call-12345678"
        with c.websocket_connect(url) as ws:
            conv = json.loads(ws.receive_text())["conversation_id"]
            ws.send_text(json.dumps({"type": "text", "text": "Hi there"}))
            for _ in range(200):
                m = ws.receive()
                if m.get("text") and json.loads(m["text"])["type"] == "metrics":
                    break
        state = asyncio.run(app.state.svc.store.get_json(f"callstate:{conv}"))
        assert state and state["history"]
        with c.websocket_connect(url) as ws:  # same call id: allowed, and the history is reloaded
            assert json.loads(ws.receive_text())["type"] == "ready"


@pytest.fixture
def redis_url():
    if not shutil.which("redis-server"):
        pytest.skip("redis-server not installed")
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    proc = subprocess.Popen(["redis-server", "--port", str(port), "--save", "", "--appendonly", "no"], stdout=subprocess.DEVNULL)
    time.sleep(0.3)
    yield f"redis://127.0.0.1:{port}/0"
    proc.terminate()
    proc.wait()


def test_limits_hold_across_instances_with_redis(redis_url):
    async def go():
        a, b = RedisStore(redis_url), RedisStore(redis_url)  # two gateway instances
        la, lb = limits(a), limits(b)
        await la.call_started("u1", "call-a")
        with pytest.raises(LimitExceeded):
            await lb.call_started("u1", "call-b")
        await a.set_json("callstate:c1", {"memory": {"location": "East Legon"}}, 60)
        assert (await b.get_json("callstate:c1"))["memory"]["location"] == "East Legon"
        await a.close()
        await b.close()

    asyncio.run(go())
