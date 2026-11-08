"""Shared state for scale-out: agentic call state and usage limits.

With REDIS_URL set, every gateway instance sees the same state, so a call that reconnects to another instance
(Vercel cuts a function off at 300 s) keeps its agent memory, and limits hold across instances. Without it, state
lives in this process, which is enough for one instance and for tests.
"""

from __future__ import annotations

import json
import time
from typing import Any, Protocol


class Store(Protocol):
    async def get_json(self, key: str) -> Any | None: ...

    async def set_json(self, key: str, value: Any, ttl_s: int) -> None: ...

    async def incr(self, key: str, amount: int, ttl_s: int) -> int: ...

    async def close(self) -> None: ...


class MemoryStore:
    def __init__(self) -> None:
        self._data: dict[str, tuple[float, Any]] = {}

    def _live(self, key: str) -> Any | None:
        item = self._data.get(key)
        if item is None:
            return None
        expires, value = item
        if expires < time.time():
            self._data.pop(key, None)
            return None
        return value

    async def get_json(self, key: str) -> Any | None:
        return self._live(key)

    async def set_json(self, key: str, value: Any, ttl_s: int) -> None:
        self._data[key] = (time.time() + ttl_s, json.loads(json.dumps(value)))
        if len(self._data) > 50_000:  # crude bound for a single POC instance
            now = time.time()
            for k in [k for k, (exp, _) in self._data.items() if exp < now]:
                self._data.pop(k, None)

    async def incr(self, key: str, amount: int, ttl_s: int) -> int:
        current = self._live(key) or 0
        expires = self._data[key][0] if key in self._data else time.time() + ttl_s
        value = int(current) + amount
        self._data[key] = (expires, value)
        return value

    async def close(self) -> None:
        self._data.clear()


class RedisStore:
    def __init__(self, url: str) -> None:
        import redis.asyncio as redis

        self._redis = redis.from_url(url)

    async def get_json(self, key: str) -> Any | None:
        raw = await self._redis.get(key)
        return None if raw is None else json.loads(raw)

    async def set_json(self, key: str, value: Any, ttl_s: int) -> None:
        await self._redis.set(key, json.dumps(value, ensure_ascii=False), ex=ttl_s)

    async def incr(self, key: str, amount: int, ttl_s: int) -> int:
        pipe = self._redis.pipeline()
        pipe.incrby(key, amount)
        pipe.expire(key, ttl_s, nx=True)
        value, _ = await pipe.execute()
        return int(value)

    async def close(self) -> None:
        await self._redis.aclose()


def build_store(redis_url: str | None) -> Store:
    return RedisStore(redis_url) if redis_url else MemoryStore()


# ------------------------------------------------------------------ limits
_SLOT_TTL_S = 90


class LimitExceeded(Exception):
    def __init__(self, what: str, retry_after_s: int) -> None:
        super().__init__(what)
        self.what = what
        self.retry_after_s = retry_after_s


class Limits:
    """Per-user fixed-window rate limits, concurrent calls and a daily voice-minute budget."""

    def __init__(self, store: Store, *, enabled: bool, per_minute: dict[str, int], max_calls: int, daily_voice_s: int) -> None:
        self.store = store
        self.enabled = enabled
        self.per_minute = per_minute
        self.max_calls = max_calls
        self.daily_voice_s = daily_voice_s

    async def hit(self, user_id: str, action: str) -> None:
        """Count one action; raise LimitExceeded when the user is over this minute's allowance."""
        limit = self.per_minute.get(action)
        if not self.enabled or not limit:
            return
        window = int(time.time() // 60)
        count = await self.store.incr(f"rl:{action}:{user_id}:{window}", 1, 120)
        if count > limit:
            raise LimitExceeded(action, 60 - int(time.time() % 60))

    async def call_started(self, user_id: str, call_id: str) -> None:
        """Take a call slot. Slots expire unless renewed by heartbeat(), so a killed function cannot leak one."""
        if not self.enabled:
            return
        if await self.voice_seconds_today(user_id) >= self.daily_voice_s:
            raise LimitExceeded("voice_minutes", _seconds_to_midnight())
        slots = await self._slots(user_id)
        if call_id not in slots and len(slots) >= self.max_calls:
            raise LimitExceeded("concurrent_calls", 15)
        slots[call_id] = time.time() + _SLOT_TTL_S
        await self.store.set_json(f"calls:{user_id}", slots, 3600)

    async def heartbeat(self, user_id: str, call_id: str, seconds: float) -> None:
        """Renew the call's slot and count the time since the last beat against today's budget."""
        if not self.enabled:
            return
        slots = await self._slots(user_id)
        slots[call_id] = time.time() + _SLOT_TTL_S
        await self.store.set_json(f"calls:{user_id}", slots, 3600)
        used = await self.store.incr(f"voice_s:{user_id}:{_today()}", int(seconds), 2 * 86400)
        if used >= self.daily_voice_s:
            raise LimitExceeded("voice_minutes", _seconds_to_midnight())

    async def call_ended(self, user_id: str, call_id: str) -> None:
        if not self.enabled:
            return
        slots = await self._slots(user_id)
        slots.pop(call_id, None)
        await self.store.set_json(f"calls:{user_id}", slots, 3600)

    async def _slots(self, user_id: str) -> dict[str, float]:
        now = time.time()
        raw = await self.store.get_json(f"calls:{user_id}") or {}
        return {k: float(v) for k, v in raw.items() if float(v) > now}

    async def voice_seconds_today(self, user_id: str) -> int:
        value = await self.store.get_json(f"voice_s:{user_id}:{_today()}")
        return int(value or 0)


def _today() -> str:
    return time.strftime("%Y%m%d", time.gmtime())


def _seconds_to_midnight() -> int:
    return 86400 - int(time.time() % 86400)
