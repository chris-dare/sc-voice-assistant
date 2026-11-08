import httpx
import pytest
import respx

from app.bus import MemoryBus, channel_for
from app.delivery import SeenSet, handle_webhook, webhook_secret_ok
from app.sunco import SunCoClient, SunCoError

BASE = "https://sunco.test/v2/apps/app1"


def client() -> SunCoClient:
    return SunCoClient("https://sunco.test", "app1", "key", "secret")


@respx.mock
async def test_ensure_user_handles_existing_user():
    respx.post(f"{BASE}/users").mock(return_value=httpx.Response(409, json={"errors": []}))
    respx.get(f"{BASE}/users/ext-1").mock(return_value=httpx.Response(200, json={"user": {"id": "u1"}}))
    assert await client().ensure_user("ext-1") == "u1"


@respx.mock
async def test_ensure_conversation_creates_when_missing():
    respx.get(f"{BASE}/conversations").mock(return_value=httpx.Response(200, json={"conversations": []}))
    create = respx.post(f"{BASE}/conversations").mock(return_value=httpx.Response(201, json={"conversation": {"id": "c1"}}))
    assert await client().ensure_conversation("u1") == "c1"
    assert create.calls.last.request.read() == b'{"type":"personal","participants":[{"userId":"u1","subscribeSDKClient":false}]}'


@respx.mock
async def test_post_text_sends_author_and_metadata_and_retries_429():
    route = respx.post(f"{BASE}/conversations/c1/messages").mock(
        side_effect=[
            httpx.Response(429),
            httpx.Response(201, json={"messages": [{"id": "m1", "author": {"type": "user"}, "content": {"type": "text", "text": "hi"}}]}),
        ]
    )
    msg = await client().post_text("c1", "u1", "hi", {"channel": "voice"})
    assert msg.id == "m1" and route.call_count == 2
    assert b'"metadata":{"channel":"voice"}' in route.calls.last.request.read()


@respx.mock
async def test_client_errors_are_not_retried():
    route = respx.get(f"{BASE}/conversations/c1/messages").mock(return_value=httpx.Response(401))
    with pytest.raises(SunCoError) as exc:
        await client().list_messages("c1")
    assert exc.value.status == 401 and route.call_count == 1


async def test_webhook_publishes_only_new_business_messages():
    bus = MemoryBus()
    seen = SeenSet()
    body = {
        "events": [
            {
                "type": "conversation:message",
                "payload": {
                    "conversation": {"id": "c1"},
                    "message": {"id": "m1", "author": {"type": "business"}, "content": {"type": "text", "text": "Hello"}},
                },
            },
            {
                "type": "conversation:message",
                "payload": {
                    "conversation": {"id": "c1"},
                    "message": {"id": "m2", "author": {"type": "user"}, "content": {"type": "text", "text": "Hi"}},
                },
            },
            {"type": "conversation:read", "payload": {}},
        ]
    }
    async with bus.subscribe(channel_for("c1")) as sub:
        assert await handle_webhook(bus, seen, body) == 1
        assert await handle_webhook(bus, seen, body) == 0  # duplicate delivery
        event = await anext(sub)
    assert event["message"]["id"] == "m1"


def test_webhook_secret_check():
    assert webhook_secret_ok("s", "s")
    assert not webhook_secret_ok("s", "x")
    assert not webhook_secret_ok("", "")
    assert not webhook_secret_ok("s", None)


@respx.mock
async def test_get_message_searches_conversation():
    respx.get(f"{BASE}/conversations/c1/messages").mock(
        return_value=httpx.Response(
            200, json={"messages": [{"id": "m9", "author": {"type": "business"}, "content": {"type": "text", "text": "x"}}]}
        )
    )
    assert (await client().get_message("c1", "m9")).text == "x"
    with pytest.raises(SunCoError) as exc:
        await client().get_message("c1", "other")
    assert exc.value.status == 404
