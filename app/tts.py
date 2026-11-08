"""TTS Adapter: ElevenLabs streaming text-to-speech, plus the read-aloud cache."""

from __future__ import annotations

import asyncio
import logging
import time
from collections import OrderedDict
from collections.abc import AsyncIterator
from typing import Protocol

import httpx

log = logging.getLogger(__name__)


class TTSError(Exception):
    pass


class TTSProvider(Protocol):
    def stream(self, text: str, model_id: str, output_format: str) -> AsyncIterator[bytes]: ...


class ElevenLabsTTS:
    def __init__(
        self, api_key: str, base_url: str, voice_id: str, http: httpx.AsyncClient, max_concurrent: int = 2, retries: int = 3
    ) -> None:
        self._key = api_key
        self._base = base_url.rstrip("/")
        self._voice = voice_id
        self._http = http
        # The ElevenLabs plan caps concurrent requests (3 today); stay under it per instance.
        self._slots = asyncio.Semaphore(max_concurrent)
        self._retries = retries

    async def stream(self, text: str, model_id: str, output_format: str) -> AsyncIterator[bytes]:
        url = f"{self._base}/v1/text-to-speech/{self._voice}/stream"
        delay = 0.3
        for attempt in range(self._retries + 1):
            async with self._slots:
                try:
                    async with self._http.stream(
                        "POST",
                        url,
                        params={"output_format": output_format},
                        headers={"xi-api-key": self._key},
                        json={"text": text, "model_id": model_id},
                        timeout=httpx.Timeout(30, connect=5),
                    ) as resp:
                        if resp.status_code == 429 and attempt < self._retries:
                            await resp.aread()
                            retry = True
                        elif resp.status_code != 200:
                            body = (await resp.aread())[:300]
                            raise TTSError(f"TTS {resp.status_code}: {body!r}")
                        else:
                            retry = False
                            async for chunk in resp.aiter_bytes():
                                if chunk:
                                    yield chunk
                except httpx.HTTPError as exc:
                    raise TTSError(f"TTS request failed: {exc}") from exc
            if not retry:
                return
            log.info("TTS rate-limited (429), retry %d in %.1fs", attempt + 1, delay)
            await asyncio.sleep(delay)
            delay *= 2


class TTSCache:
    """Read-aloud cache keyed by message, voice and model (AD-06). Redis when configured."""

    def __init__(self, max_items: int, ttl_s: int, redis_url: str | None = None) -> None:
        self._max = max_items
        self._ttl = ttl_s
        self._mem: OrderedDict[str, tuple[float, bytes, str]] = OrderedDict()
        self._redis = None
        if redis_url:
            import redis.asyncio as redis

            self._redis = redis.from_url(redis_url)

    async def get(self, key: str) -> tuple[bytes, str] | None:
        if self._redis is not None:
            data = await self._redis.hgetall(f"tts:{key}")
            if data:
                return data[b"audio"], data[b"type"].decode()
            return None
        hit = self._mem.get(key)
        if not hit:
            return None
        expires, audio, ctype = hit
        if expires < time.monotonic():
            self._mem.pop(key, None)
            return None
        self._mem.move_to_end(key)
        return audio, ctype

    async def put(self, key: str, audio: bytes, content_type: str) -> None:
        if self._redis is not None:
            await self._redis.hset(f"tts:{key}", mapping={"audio": audio, "type": content_type})
            await self._redis.expire(f"tts:{key}", self._ttl)
            return
        self._mem[key] = (time.monotonic() + self._ttl, audio, content_type)
        self._mem.move_to_end(key)
        while len(self._mem) > self._max:
            self._mem.popitem(last=False)

    async def close(self) -> None:
        if self._redis is not None:
            await self._redis.aclose()


def content_type_for(output_format: str) -> str:
    if output_format.startswith("mp3"):
        return "audio/mpeg"
    if output_format.startswith("opus"):
        return "audio/ogg"
    return "audio/L16"
