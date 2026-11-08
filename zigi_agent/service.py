"""The agent service: POST /v1/turn streams one turn as NDJSON. Stateless: the gateway sends the call's state.

Run on its own (uvicorn zigi_agent.service:app) or, for local dev and tests, in-process through AgentEngine.
"""

from __future__ import annotations

import hmac
import json
import logging
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, Literal

import logfire
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.sunco import ConversationProvider, SunCoClient

from .agent import CallDeps, build_agent
from .config import AgentSettings, get_settings
from .mock_model import mock_model
from .rules import state_from_dict
from .turn import run_turn

log = logging.getLogger(__name__)


class TurnRequest(BaseModel):
    conversation_id: str
    sunco_user_id: str
    kind: Literal["user", "push"] = "user"
    text: str = ""
    history: list[Any] = Field(default_factory=list)
    zigi_state: dict[str, Any] | None = None
    grounding: list[str] = Field(default_factory=list)
    memory: dict[str, str] = Field(default_factory=dict)


class AgentEngine:
    """The agent plus what its tools need. One per process."""

    def __init__(self, settings: AgentSettings, provider: ConversationProvider | None = None) -> None:
        self.settings = settings
        self._own_provider = provider is None
        if provider is None:
            if settings.mock_providers:
                from app.mocks import MockSunCo

                provider = MockSunCo()
            else:
                provider = SunCoClient(
                    settings.sunco_base_url,
                    settings.zendesk_app_id,
                    settings.zendesk_key_id,
                    settings.zendesk_secret,
                    settings.sunco_timeout_s,
                )
        self.provider = provider
        self.agent = build_agent(settings, mock_model() if settings.mock_providers else None)
        self.skip = [re.compile(p, re.IGNORECASE) for p in settings.speech_skip_patterns]
        self.preamble = [re.compile(p, re.IGNORECASE) for p in settings.zigi_preamble_patterns]

    async def turn(self, req: TurnRequest) -> AsyncIterator[dict[str, Any]]:
        async def _noop(_: dict[str, Any]) -> None:
            return None

        deps = CallDeps(
            provider=self.provider,
            settings=self.settings,
            conversation_id=req.conversation_id,
            sunco_user_id=req.sunco_user_id,
            user_said=req.text,
            zigi=state_from_dict(req.zigi_state),
            grounding=list(req.grounding[-80:]),
            memory=dict(req.memory),
            skip=self.skip,
            preamble=self.preamble,
            emit=_noop,
        )
        async for event in run_turn(self.agent, deps, kind=req.kind, text=req.text, history=req.history):
            yield event

    async def aclose(self) -> None:
        if self._own_provider:
            await self.provider.aclose()


def create_app(settings: AgentSettings | None = None) -> FastAPI:
    cfg = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):  # noqa: ANN202
        logfire.configure(service_name="zigi-agent", environment=cfg.environment, send_to_logfire="if-token-present", console=False)
        # Prompts, replies and tool arguments carry what callers said: keep them out of traces (design 6.2).
        logfire.instrument_pydantic_ai(include_content=False)
        logfire.instrument_httpx(capture_headers=False)
        app.state.engine = AgentEngine(cfg)
        yield
        await app.state.engine.aclose()

    app = FastAPI(title="Zigi agent service", lifespan=lifespan)

    def authorised(authorization: str | None = Header(default=None)) -> None:
        token = cfg.service_token
        if not token:
            if cfg.mock_providers:
                return
            raise HTTPException(503, "agent service token not configured")
        given = (authorization or "").removeprefix("Bearer ").strip()
        if not hmac.compare_digest(given, token):
            raise HTTPException(401, "bad token")

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok", "model": cfg.agent_model}

    @app.post("/v1/turn", dependencies=[Depends(authorised)])
    async def turn(req: TurnRequest) -> StreamingResponse:
        engine: AgentEngine = app.state.engine

        async def body() -> AsyncIterator[bytes]:
            async for event in engine.turn(req):
                yield (json.dumps(event, ensure_ascii=False) + "\n").encode()

        return StreamingResponse(body(), media_type="application/x-ndjson")

    return app


app = create_app()
