"""How bot messages reach the gateway: SunCo webhook (production) or polling (local dev).

Either way, new business-authored messages are published on the bus, keyed by conversation.
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import logging
from collections import OrderedDict
from collections.abc import AsyncIterator
from typing import Any

from .bus import Bus, channel_for
from .sunco import ConversationProvider, Message, SunCoError

log = logging.getLogger(__name__)


class SeenSet:
    """Bounded set of message IDs, so retries and duplicate webhooks are dropped."""

    def __init__(self, max_items: int = 5000) -> None:
        self._ids: OrderedDict[str, None] = OrderedDict()
        self._max = max_items

    def add(self, msg_id: str) -> bool:
        """True if the ID is new."""
        if msg_id in self._ids:
            return False
        self._ids[msg_id] = None
        while len(self._ids) > self._max:
            self._ids.popitem(last=False)
        return True


async def publish_bot_message(bus: Bus, msg: Message) -> None:
    await bus.publish(channel_for(msg.conversation_id), {"kind": "bot_message", "message": msg.to_dict()})


class Poller:
    """Polls a conversation for new bot messages while at least one session watches it."""

    def __init__(self, provider: ConversationProvider, bus: Bus, interval_s: float) -> None:
        self._provider = provider
        self._bus = bus
        self._interval = interval_s
        self._watchers: dict[str, int] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}

    @contextlib.asynccontextmanager
    async def watch(self, conversation_id: str) -> AsyncIterator[None]:
        self._watchers[conversation_id] = self._watchers.get(conversation_id, 0) + 1
        if conversation_id not in self._tasks:
            seen = SeenSet()
            for m in await self._provider.list_messages(conversation_id):
                seen.add(m.id)
            self._tasks[conversation_id] = asyncio.create_task(self._loop(conversation_id, seen))
        try:
            yield
        finally:
            self._watchers[conversation_id] -= 1
            if self._watchers[conversation_id] <= 0:
                self._watchers.pop(conversation_id, None)
                task = self._tasks.pop(conversation_id, None)
                if task:
                    task.cancel()

    async def _loop(self, conversation_id: str, seen: SeenSet) -> None:
        backoff = self._interval
        while True:
            try:
                for m in await self._provider.list_messages(conversation_id):
                    if seen.add(m.id) and m.author_type == "business":
                        await publish_bot_message(self._bus, m)
                backoff = self._interval
            except asyncio.CancelledError:
                raise
            except SunCoError as exc:
                log.warning("poll %s failed: %s", conversation_id, exc)
                backoff = min(backoff * 2, 5.0)
            await asyncio.sleep(backoff)

    async def close(self) -> None:
        for task in self._tasks.values():
            task.cancel()
        self._tasks.clear()


class NoopWatcher:
    @contextlib.asynccontextmanager
    async def watch(self, conversation_id: str) -> AsyncIterator[None]:
        yield

    async def close(self) -> None:
        return None


def webhook_secret_ok(expected: str, provided: str | None) -> bool:
    return bool(expected) and provided is not None and hmac.compare_digest(expected, provided)


async def handle_webhook(bus: Bus, seen: SeenSet, body: dict[str, Any]) -> int:
    """Publish business messages from a SunCo v2 webhook body. Returns how many were published."""
    published = 0
    for event in body.get("events") or []:
        if event.get("type") != "conversation:message":
            continue
        payload = event.get("payload") or {}
        conv_id = (payload.get("conversation") or {}).get("id")
        raw = payload.get("message") or {}
        if not conv_id or not raw.get("id"):
            continue
        msg = Message.from_api(conv_id, raw)
        if msg.author_type != "business" or not seen.add(msg.id):
            continue
        await publish_bot_message(bus, msg)
        published += 1
    return published
