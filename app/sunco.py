"""Conversation Client: the gateway's only path to the bot (AD-01).

Talks to the Sunshine Conversations v2 API with the app's basic-auth key.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

log = logging.getLogger(__name__)

_RETRY_STATUS = {429, 500, 502, 503, 504}


class SunCoError(Exception):
    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


@dataclass(slots=True)
class Message:
    id: str
    conversation_id: str
    author_type: str  # "user" | "business"
    received: str
    content: dict[str, Any]
    author_name: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def text(self) -> str:
        return str(self.content.get("text") or "")

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "conversation_id": self.conversation_id,
            "author_type": self.author_type,
            "author_name": self.author_name,
            "received": self.received,
            "content": self.content,
            "metadata": self.metadata,
        }

    @classmethod
    def from_api(cls, conversation_id: str, raw: dict[str, Any]) -> Message:
        author = raw.get("author") or {}
        return cls(
            id=raw["id"],
            conversation_id=conversation_id,
            author_type=author.get("type", "business"),
            author_name=author.get("displayName"),
            received=raw.get("received", ""),
            content=raw.get("content") or {},
            metadata=raw.get("metadata") or {},
        )

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Message:
        return cls(
            id=data["id"],
            conversation_id=data["conversation_id"],
            author_type=data.get("author_type", "business"),
            author_name=data.get("author_name"),
            received=data.get("received", ""),
            content=data.get("content") or {},
            metadata=data.get("metadata") or {},
        )


class ConversationProvider(Protocol):
    async def ensure_user(self, external_id: str) -> str: ...
    async def ensure_conversation(self, user_id: str) -> str: ...
    async def post_text(self, conversation_id: str, user_id: str, text: str, metadata: dict[str, Any] | None = None) -> Message: ...
    async def list_messages(self, conversation_id: str) -> list[Message]: ...
    async def get_message(self, conversation_id: str, message_id: str) -> Message: ...
    async def aclose(self) -> None: ...


class SunCoClient:
    def __init__(
        self,
        base_url: str,
        app_id: str,
        key_id: str,
        secret: str,
        timeout_s: float = 10.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._http = httpx.AsyncClient(
            base_url=f"{base_url.rstrip('/')}/v2/apps/{app_id}",
            auth=(key_id, secret),
            timeout=timeout_s,
            transport=transport,
            headers={"user-agent": "sc-voice-gateway/0.1"},
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _request(self, method: str, path: str, *, ok: tuple[int, ...] = (200, 201), **kw: Any) -> httpx.Response:
        delay = 0.25
        for attempt in range(3):
            try:
                resp = await self._http.request(method, path, **kw)
            except httpx.TransportError as exc:
                if attempt == 2:
                    raise SunCoError(f"SunCo unreachable: {exc}") from exc
            else:
                if resp.status_code in ok:
                    return resp
                if resp.status_code not in _RETRY_STATUS or attempt == 2:
                    raise SunCoError(f"SunCo {method} {path} -> {resp.status_code}: {resp.text[:300]}", resp.status_code)
            await asyncio.sleep(delay)
            delay *= 2
        raise AssertionError("unreachable")

    async def ensure_user(self, external_id: str) -> str:
        # Look up first: returning users are the common case, and a 409 on create is logged as an error.
        resp = await self._request("GET", f"/users/{external_id}", ok=(200, 404))
        if resp.status_code == 404:
            resp = await self._request("POST", "/users", json={"externalId": external_id}, ok=(200, 201, 409))
            if resp.status_code == 409:  # created concurrently
                resp = await self._request("GET", f"/users/{external_id}")
        return resp.json()["user"]["id"]

    async def ensure_conversation(self, user_id: str) -> str:
        resp = await self._request("GET", "/conversations", params={"filter[userId]": user_id})
        convs = resp.json().get("conversations") or []
        personal = [c for c in convs if c.get("type") == "personal"] or convs
        if personal:
            return personal[0]["id"]
        resp = await self._request(
            "POST",
            "/conversations",
            json={"type": "personal", "participants": [{"userId": user_id, "subscribeSDKClient": False}]},
        )
        return resp.json()["conversation"]["id"]

    async def post_text(self, conversation_id: str, user_id: str, text: str, metadata: dict[str, Any] | None = None) -> Message:
        body: dict[str, Any] = {
            "author": {"type": "user", "userId": user_id},
            "content": {"type": "text", "text": text},
        }
        if metadata:
            body["metadata"] = metadata
        resp = await self._request("POST", f"/conversations/{conversation_id}/messages", json=body)
        return Message.from_api(conversation_id, resp.json()["messages"][0])

    async def list_messages(self, conversation_id: str) -> list[Message]:
        resp = await self._request("GET", f"/conversations/{conversation_id}/messages")
        return [Message.from_api(conversation_id, m) for m in resp.json().get("messages") or []]

    async def get_message(self, conversation_id: str, message_id: str) -> Message:
        # The v2 API has no GET for a single message (it answers 404), so search the latest page.
        for msg in await self.list_messages(conversation_id):
            if msg.id == message_id:
                return msg
        raise SunCoError(f"message {message_id} not found in conversation", 404)
