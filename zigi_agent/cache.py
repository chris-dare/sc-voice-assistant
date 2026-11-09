"""Semantic cache for Zigi's FAQ answers (performance).

Zigi takes 5 to 17 s per answer, and many callers ask the same general questions ("how does roaming work?",
"how do I swap my SIM?"). When a general question is close in meaning to one Zigi answered recently, the agent
uses that answer instead of asking Zigi again.

Only general questions and answers are cached: never while Zigi waits for input (OTP, number, confirmation),
never a question about the caller's own account or a transaction, never an answer that greets the caller by
name, asks for input, or reports an error. Entries expire (prices change). Cached turns do not reach Zigi, so
they are missing from SunCo's history: a trade-off logged on each hit.
"""

from __future__ import annotations

import logging
import math
import re
import time
from dataclasses import dataclass
from typing import Any

import httpx

from app.store import Store

log = logging.getLogger(__name__)

_PERSONAL = re.compile(
    r"\b(my|mine|me|i am|i'm|i have|i've|i want|i'd like|buy|purchase|send|transfer|pay|top ?up|recharge|"
    r"balance|account|otp|pin|password|login|log in|register|block|unblock|cancel|activate|deactivate|subscribe)\b",
    re.I,
)
_ANSWER_UNSAFE = re.compile(
    r"\b(error|unable|couldn't|could not|try again|issue|please enter|enter your|provide your|your number|"
    r"otp|logging you in|reply yes|confirm)\b|\bHi [A-Z]{3,}\b",
    re.I,
)
_ASKS = re.compile(
    r"\b(please (enter|provide|share|tell|send|select|choose|reply|let me know)|let me know|which (area|town|one|option)|"
    r"what (area|town|is your)|kindly (enter|provide|share))\b",
    re.I,
)
_WORD = re.compile(r"[a-z0-9]+")
_STOP = {
    "the",
    "a",
    "an",
    "is",
    "are",
    "do",
    "does",
    "how",
    "what",
    "can",
    "i",
    "you",
    "to",
    "of",
    "for",
    "on",
    "in",
    "please",
    "tell",
    "me",
    "about",
}


def normalise(text: str) -> str:
    return " ".join(w for w in _WORD.findall(text.lower()) if w not in _STOP)


def question_cacheable(text: str, waiting_for: str) -> bool:
    words = _WORD.findall(text.lower())
    return waiting_for == "none" and 3 <= len(words) <= 30 and not _PERSONAL.search(text) and not re.search(r"\d{4,}", text)


def answer_cacheable(result: dict[str, Any]) -> bool:
    msgs = result.get("zigi_messages") or []
    text = " ".join(m.get("text", "") for m in msgs)
    last = (msgs[-1].get("text", "") if msgs else "").strip()
    return (
        bool(msgs)
        and not result.get("timed_out")
        and result.get("waiting_for") == "none"
        and len(text) >= 40
        and not _ANSWER_UNSAFE.search(text)
        # An answer that asks the caller something is the start of a flow, not an answer (seen 09 Nov in the evals:
        # "Which area or town should I check?" was served from cache to the next caller).
        and not last.rstrip().endswith("?")
        and not _ASKS.search(text)
        and not result.get("options")
    )


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


@dataclass
class CacheHit:
    question: str
    result: dict[str, Any]
    similarity: float


class Embedder:
    """OpenRouter embeddings (openai/text-embedding-3-small, 256 dimensions)."""

    def __init__(self, api_key: str, model: str, http: httpx.AsyncClient, base_url: str = "https://openrouter.ai/api/v1") -> None:
        self.api_key, self.model, self.http, self.url = api_key, model, http, base_url.rstrip("/") + "/embeddings"

    async def embed(self, text: str) -> list[float] | None:
        try:
            resp = await self.http.post(
                self.url,
                json={"model": self.model, "input": [text], "dimensions": 256},
                headers={"Authorization": f"Bearer {self.api_key}"},
                timeout=3,
            )
            resp.raise_for_status()
            return [round(float(x), 5) for x in resp.json()["data"][0]["embedding"]]
        except (httpx.HTTPError, KeyError, IndexError, ValueError) as exc:
            log.warning("embedding failed: %s", type(exc).__name__)
            return None


class SemanticCache:
    """Entries live in the shared Store (Redis in production) under one key, newest last, capped."""

    KEY = "zigi:semcache:v1"

    def __init__(self, store: Store, embedder: Embedder | None, *, ttl_s: int, threshold: float, max_entries: int = 200) -> None:
        self.store, self.embedder = store, embedder
        self.ttl_s, self.threshold, self.max_entries = ttl_s, threshold, max_entries
        self._local: list[dict[str, Any]] = []
        self._loaded = 0.0

    async def _entries(self, fresh: bool = False) -> list[dict[str, Any]]:
        """A local copy, refreshed from the shared store every 30 s, so a lookup costs no round trip."""
        now = time.time()
        if fresh or now - self._loaded > 30:
            self._local = list(await self.store.get_json(self.KEY) or [])
            self._loaded = now
        return [e for e in self._local if e.get("expires", 0) > now]

    async def lookup(self, question: str) -> tuple[CacheHit | None, list[float] | None]:
        entries = await self._entries()
        key = normalise(question)
        for e in reversed(entries):
            if e["norm"] == key:
                return CacheHit(e["question"], e["result"], 1.0), None
        if not entries or self.embedder is None:
            return None, None
        vec = await self.embedder.embed(question)
        if vec is None:
            return None, None
        best = max(entries, key=lambda e: _cosine(vec, e["vec"]) if e.get("vec") else -1.0)
        score = _cosine(vec, best["vec"]) if best.get("vec") else 0.0
        if score >= self.threshold:
            return CacheHit(best["question"], best["result"], score), vec
        return None, vec

    async def store_answer(self, question: str, result: dict[str, Any], vec: list[float] | None) -> None:
        if vec is None and self.embedder is not None:
            vec = await self.embedder.embed(question)
        entries = await self._entries(fresh=True)
        entries = [e for e in entries if e["norm"] != normalise(question)]
        entries.append(
            {"question": question, "norm": normalise(question), "vec": vec, "result": result, "expires": time.time() + self.ttl_s}
        )
        self._local = entries[-self.max_entries :]
        await self.store.set_json(self.KEY, self._local, self.ttl_s)
