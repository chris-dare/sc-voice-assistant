"""Fan-out of bot messages to whichever replica holds the session (AD-05).

MemoryBus serves a single process. RedisBus routes across replicas with pub/sub.
Channels are keyed by SunCo conversation ID.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections import defaultdict
from collections.abc import AsyncIterator
from typing import Any, Protocol

log = logging.getLogger(__name__)


def channel_for(conversation_id: str) -> str:
    return f"conv:{conversation_id}"


class Bus(Protocol):
    async def publish(self, channel: str, data: dict[str, Any]) -> None: ...

    def subscribe(self, channel: str) -> contextlib.AbstractAsyncContextManager[AsyncIterator[dict[str, Any]]]: ...

    async def close(self) -> None: ...


async def _drain(queue: asyncio.Queue[dict[str, Any]]) -> AsyncIterator[dict[str, Any]]:
    while True:
        yield await queue.get()


class MemoryBus:
    def __init__(self) -> None:
        self._subs: dict[str, set[asyncio.Queue[dict[str, Any]]]] = defaultdict(set)

    async def publish(self, channel: str, data: dict[str, Any]) -> None:
        for q in list(self._subs.get(channel, ())):
            q.put_nowait(data)

    @contextlib.asynccontextmanager
    async def subscribe(self, channel: str) -> AsyncIterator[AsyncIterator[dict[str, Any]]]:
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._subs[channel].add(q)
        try:
            yield _drain(q)
        finally:
            self._subs[channel].discard(q)
            if not self._subs[channel]:
                self._subs.pop(channel, None)

    async def close(self) -> None:
        self._subs.clear()


class RedisBus:
    def __init__(self, url: str) -> None:
        import redis.asyncio as redis

        self._redis = redis.from_url(url)

    async def publish(self, channel: str, data: dict[str, Any]) -> None:
        await self._redis.publish(channel, json.dumps(data))

    @contextlib.asynccontextmanager
    async def subscribe(self, channel: str) -> AsyncIterator[AsyncIterator[dict[str, Any]]]:
        pubsub = self._redis.pubsub()
        await pubsub.subscribe(channel)

        async def messages() -> AsyncIterator[dict[str, Any]]:
            async for msg in pubsub.listen():
                if msg.get("type") != "message":
                    continue
                try:
                    yield json.loads(msg["data"])
                except (TypeError, ValueError):
                    log.warning("dropping malformed bus message on %s", channel)

        try:
            yield messages()
        finally:
            with contextlib.suppress(Exception):
                await pubsub.unsubscribe(channel)
                await pubsub.aclose()

    async def close(self) -> None:
        await self._redis.aclose()
