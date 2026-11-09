import asyncio

from app import redisconn
from app.bus import RedisBus
from app.store import RedisStore


def test_upstash_urls_are_upgraded_to_tls():
    assert redisconn.normalise("redis://default:pw@eu1-abc.upstash.io:6379").startswith("rediss://")
    assert redisconn.normalise(' "rediss://default:pw@eu1-abc.upstash.io:6379" ').startswith("rediss://default")
    assert redisconn.normalise("redis://localhost:6379") == "redis://localhost:6379"


def test_describe_never_shows_the_password():
    text = redisconn.describe("rediss://default:s3cretvalue@eu1-abc.upstash.io:6379")
    assert "s3cretvalue" not in text and "password=yes" in text and "scheme=rediss" in text


def test_unreachable_redis_falls_back_to_memory():
    redisconn.reset()

    async def go():
        url = "redis://127.0.0.1:1"
        store, bus = RedisStore(url), RedisBus(url)
        assert await store.incr("k", 2, 60) == 2
        await store.set_json("j", {"a": 1}, 60)
        got = await store.get_json("j")
        async with bus.subscribe("c") as messages:
            await bus.publish("c", {"x": 1})
            first = await asyncio.wait_for(messages.__anext__(), 2)
        await store.close()
        return got, first

    got, first = asyncio.run(go())
    assert got == {"a": 1} and first == {"x": 1}
    redisconn.reset()
