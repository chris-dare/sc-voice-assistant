"""The gateway's client for the Zigi agent service (design 5.8): one turn in, a stream of events out.

AGENT_SERVICE_URL set: HTTP to the separate service (NDJSON stream). Unset: the same service code in-process,
for local dev and tests.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any, Protocol

import httpx


class AgentServiceError(Exception):
    pass


class AgentClient(Protocol):
    def turn(self, request: dict[str, Any]) -> AsyncIterator[dict[str, Any]]: ...

    async def aclose(self) -> None: ...


class HTTPAgentClient:
    def __init__(self, base_url: str, token: str, http: httpx.AsyncClient) -> None:
        self.url = base_url.rstrip("/") + "/v1/turn"
        self.token = token
        self.http = http

    async def turn(self, request: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
        headers = {"Authorization": f"Bearer {self.token}"}
        try:
            async with self.http.stream("POST", self.url, json=request, headers=headers, timeout=httpx.Timeout(10, read=60)) as resp:
                if resp.status_code != 200:
                    raise AgentServiceError(f"agent service HTTP {resp.status_code}")
                async for line in resp.aiter_lines():
                    if line.strip():
                        yield json.loads(line)
        except httpx.HTTPError as exc:
            raise AgentServiceError(f"agent service unreachable: {type(exc).__name__}") from exc

    async def aclose(self) -> None:
        return None


class InProcessAgentClient:
    """Runs the agent service's engine in this process, sharing the gateway's SunCo provider."""

    def __init__(self, provider: Any, mock: bool) -> None:
        from zigi_agent.config import AgentSettings
        from zigi_agent.service import AgentEngine

        settings = AgentSettings(mock_providers=mock)
        self.engine = AgentEngine(settings, provider=provider)

    async def turn(self, request: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
        from zigi_agent.service import TurnRequest

        async for event in self.engine.turn(TurnRequest(**request)):
            yield event

    async def aclose(self) -> None:
        await self.engine.aclose()
