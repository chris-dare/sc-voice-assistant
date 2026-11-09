"""One way to reach Redis for the store, the bus and the TTS cache, with a safe fallback.

- Upstash only accepts TLS. A redis:// URL for an upstash.io host is upgraded to rediss://.
- The first use pings Redis. If that fails, this process logs why (never the password) and uses in-process
  memory instead, so a Redis outage or a bad REDIS_URL degrades to single-instance behaviour rather than
  failing every request.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any
from urllib.parse import urlsplit, urlunsplit

log = logging.getLogger(__name__)

_status: dict[str, bool] = {}
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

    return redis.from_url(normalise(url), socket_connect_timeout=3, health_check_interval=30)


async def available(url: str, conn: Any) -> bool:
    """True once a ping succeeds; False for the life of this process after the first failure."""
    if url in _status:
        return _status[url]
    lock = _locks.setdefault(url, asyncio.Lock())
    async with lock:
        if url not in _status:
            try:
                await asyncio.wait_for(conn.ping(), 5)
                _status[url] = True
                log.info("Redis reachable (%s)", describe(normalise(url)))
            except Exception as exc:  # noqa: BLE001 - any failure means: fall back to memory
                mark_down(url, exc)
    return _status[url]


def mark_down(url: str, exc: BaseException) -> None:
    if _status.get(url) is not False:
        log.error(
            "Redis unavailable, using in-process memory (single instance): %s: %s [%s]",
            type(exc).__name__,
            str(exc)[:200],
            describe(normalise(url)),
        )
    _status[url] = False


def reset() -> None:
    """For tests."""
    _status.clear()
    _locks.clear()
