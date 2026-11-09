"""One way to reach Redis for the store, the bus and the TTS cache, with a safe fallback.

- Upstash only accepts TLS. A redis:// URL for an upstash.io host is upgraded to rediss://.
- The first use pings Redis. If that or any later call fails, this process logs why (never the password) and
  uses in-process memory for the next 30 s, then tries Redis again. A Redis outage or a bad REDIS_URL degrades
  to single-instance behaviour rather than failing requests, and a passing hiccup does not last (seen 09 Nov: one
  read timeout left a gateway instance on memory for good).
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any
from urllib.parse import urlsplit, urlunsplit

log = logging.getLogger(__name__)

_RETRY_S = 30.0
_status: dict[str, bool] = {}
_retry_at: dict[str, float] = {}
_locks: dict[str, asyncio.Lock] = {}


def normalise(url: str) -> str:
    url = url.strip().strip('"').strip("'")
    parts = urlsplit(url)
    if parts.scheme == "redis" and (parts.hostname or "").endswith(".upstash.io"):
        parts = parts._replace(scheme="rediss")
    return urlunsplit(parts)


def describe(url: str) -> str:
    """What the URL looks like, without secrets: scheme, host suffix, port, whether it has a user and password."""
    try:
        p = urlsplit(url.strip())
        host = p.hostname or ""
        return (
            f"scheme={p.scheme or 'none'} host=*{host[-14:]} port={p.port} "
            f"user={'yes' if p.username else 'no'} password={'yes' if p.password else 'no'} path={p.path or '/'}"
        )
    except ValueError:
        return "unparseable URL"


def client(url: str) -> Any:
    import redis.asyncio as redis
    from redis.asyncio.retry import Retry
    from redis.backoff import ExponentialBackoff
    from redis.exceptions import ConnectionError as RedisConnectionError
    from redis.exceptions import TimeoutError as RedisTimeoutError

    # On serverless, a pooled connection can go stale while the function is frozen; the first read after that
    # timed out (09 Nov). Reconnect and retry twice, quickly, before giving up.
    return redis.from_url(
        normalise(url),
        socket_connect_timeout=3,
        socket_keepalive=True,
        health_check_interval=15,
        retry=Retry(ExponentialBackoff(cap=0.5, base=0.05), 2),
        retry_on_error=[RedisConnectionError, RedisTimeoutError, OSError],
    )


async def available(url: str, conn: Any) -> bool:
    """True while Redis answers; after a failure, False until a ping succeeds again (tried every 30 s)."""
    if _status.get(url) is True:
        return True
    if _status.get(url) is False and time.monotonic() < _retry_at.get(url, 0.0):
        return False
    lock = _locks.setdefault(url, asyncio.Lock())
    async with lock:
        if _status.get(url) is True:
            return True
        if _status.get(url) is False and time.monotonic() < _retry_at.get(url, 0.0):
            return False
        was_down = _status.get(url) is False
        try:
            await asyncio.wait_for(conn.ping(), 5)
            _status[url] = True
            log.info("Redis %s (%s)", "reachable again" if was_down else "reachable", describe(normalise(url)))
        except Exception as exc:  # noqa: BLE001 - any failure means: fall back to memory for a while
            mark_down(url, exc)
    return _status[url]


def mark_down(url: str, exc: BaseException) -> None:
    if _status.get(url) is not False:
        log.error(
            "Redis unavailable, using in-process memory for %ds: %s: %s [%s]",
            int(_RETRY_S),
            type(exc).__name__,
            str(exc)[:200],
            describe(normalise(url)),
        )
    _status[url] = False
    _retry_at[url] = time.monotonic() + _RETRY_S


def reset() -> None:
    """For tests."""
    _status.clear()
    _retry_at.clear()
    _locks.clear()
