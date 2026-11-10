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

import httpx
import logfire
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.bus import Bus, RedisBus
from app.store import build_store
from app.sunco import ConversationProvider, SunCoClient

from .agent import CallDeps, build_agent
from .cache import Embedder, SemanticCache
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
    sends: list[dict[str, Any]] = Field(default_factory=list)
    note: str = ""  # from the gateway, e.g. the caller spoke over the last reply


class AgentEngine:
    """The agent plus what its tools need. One per process."""

    def __init__(self, settings: AgentSettings, provider: ConversationProvider | None = None, bus: Bus | None = None) -> None:
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
        self.reader = None
        if settings.zigi_reader_enabled and not settings.mock_providers and settings.openrouter_api_key:
            from .agent import build_model, model_settings
            from .reader import ZigiReader

            reader_settings = {**model_settings(settings), "max_tokens": 400}
            self.reader = ZigiReader(build_model(settings, settings.zigi_reader_model), settings.zigi_reader_timeout_s, reader_settings)
        self.skip = [re.compile(p, re.IGNORECASE) for p in settings.speech_skip_patterns]
        self.preamble = [re.compile(p, re.IGNORECASE) for p in settings.zigi_preamble_patterns]
        self.bus = bus if bus is not None else (RedisBus(settings.redis_url) if settings.redis_url else None)
        self._own_bus = bus is None and self.bus is not None
        self.store = build_store(settings.redis_url)
        self.http = httpx.AsyncClient()
        self.cache: SemanticCache | None = None
        if settings.semantic_cache_enabled:
            embedder = (
                None
                if settings.mock_providers or not settings.openrouter_api_key
                else Embedder(settings.openrouter_api_key, settings.embedding_model, self.http)
            )
            self.cache = SemanticCache(
                self.store, embedder, ttl_s=settings.semantic_cache_ttl_s, threshold=settings.semantic_cache_threshold
            )

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
            sends=list(req.sends[-10:]),
            skip=self.skip,
            preamble=self.preamble,
            bus=self.bus,
            cache=self.cache,
            reader=self.reader,
            emit=_noop,
        )
        async for event in run_turn(self.agent, deps, kind=req.kind, text=req.text, history=req.history, note=req.note):
            yield event

    async def aclose(self) -> None:
        if self._own_provider:
            await self.provider.aclose()
        if self._own_bus and self.bus is not None:
            await self.bus.close()
        await self.store.close()
        await self.http.aclose()


def create_app(settings: AgentSettings | None = None) -> FastAPI:
    cfg = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):  # noqa: ANN202
        logfire.configure(service_name="zigi-agent", environment=cfg.environment, send_to_logfire="if-token-present", console=False)
        # Prompts, replies and tool arguments carry what callers said: keep them out of traces (design 6.2).
        logfire.instrument_pydantic_ai(include_content=False)
        logfire.instrument_httpx(capture_headers=False)
        # Our own log lines (Redis status, cache, guardrails) go to Logfire too.
        logging.basicConfig(level=logging.INFO, handlers=[logfire.LogfireLoggingHandler()], force=True)
        for noisy in ("httpx", "httpcore"):
            logging.getLogger(noisy).setLevel(logging.WARNING)
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
