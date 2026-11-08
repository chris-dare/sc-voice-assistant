"""Wiring: builds the providers once per process and resolves users to SunCo conversations."""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field

import httpx

from .agent_client import AgentClient, HTTPAgentClient, InProcessAgentClient
from .auth import external_id_for
from .bus import Bus, MemoryBus, RedisBus
from .config import Settings
from .delivery import NoopWatcher, Poller, SeenSet
from .mocks import MockSTT, MockSunCo, MockTTS
from .store import Limits, Store, build_store
from .stt import ElevenLabsSTT, STTProvider
from .sunco import ConversationProvider, SunCoClient
from .tts import ElevenLabsTTS, TTSCache, TTSProvider

log = logging.getLogger(__name__)


@dataclass(slots=True, frozen=True)
class UserContext:
    user_id: str  # gateway user ID, from the verified token
    sunco_user_id: str
    conversation_id: str


@dataclass
class Services:
    settings: Settings
    provider: ConversationProvider
    stt: STTProvider
    tts: TTSProvider
    bus: Bus
    watcher: Poller | NoopWatcher
    cache: TTSCache
    http: httpx.AsyncClient
    agent: AgentClient | None = None
    store: Store | None = None
    limits: Limits | None = None
    webhook_seen: SeenSet = field(default_factory=SeenSet)
    skip_patterns: list[re.Pattern[str]] = field(default_factory=list)
    _users: dict[str, UserContext] = field(default_factory=dict)
    _locks: dict[str, asyncio.Lock] = field(default_factory=dict)

    async def resolve(self, user_id: str) -> UserContext:
        if ctx := self._users.get(user_id):
            return ctx
        lock = self._locks.setdefault(user_id, asyncio.Lock())
        async with lock:
            if ctx := self._users.get(user_id):
                return ctx
            # Shared across instances, so a new instance skips two SunCo lookups per user.
            if self.store is not None and (cached := await self.store.get_json(f"user:{user_id}")):
                ctx = UserContext(user_id, cached["sunco_user_id"], cached["conversation_id"])
            else:
                sunco_user = await self.provider.ensure_user(external_id_for(user_id))
                conv = await self.provider.ensure_conversation(sunco_user)
                ctx = UserContext(user_id, sunco_user, conv)
                if self.store is not None:
                    await self.store.set_json(f"user:{user_id}", {"sunco_user_id": sunco_user, "conversation_id": conv}, 7 * 86400)
            self._users[user_id] = ctx
            return ctx

    def speakable(self, text: str) -> bool:
        stripped = text.strip()
        return bool(stripped) and not any(p.search(stripped) for p in self.skip_patterns)

    async def aclose(self) -> None:
        if self.store is not None:
            await self.store.close()
        if self.agent is not None:
            await self.agent.aclose()
        await self.watcher.close()
        await self.provider.aclose()
        await self.bus.close()
        await self.cache.close()
        await self.http.aclose()


def build_services(settings: Settings) -> Services:
    http = httpx.AsyncClient(timeout=30)
    provider: ConversationProvider
    stt: STTProvider
    tts: TTSProvider
    if settings.mock_providers:
        log.warning("MOCK_PROVIDERS=true: using the fake bot, STT and TTS")
        provider, stt, tts = MockSunCo(), MockSTT(), MockTTS()
    else:
        if not settings.sunco_configured:
            raise RuntimeError("Set ZENDESK_APP_ID, ZENDESK_KEY_ID and ZENDESK_SECRET (or MOCK_PROVIDERS=true)")
        if not settings.elevenlabs_configured:
            raise RuntimeError("Set ELEVENLABS_API_KEY (or MOCK_PROVIDERS=true)")
        provider = SunCoClient(
            settings.sunco_base_url,
            settings.zendesk_app_id,
            settings.zendesk_key_id,
            settings.zendesk_secret,
            settings.sunco_timeout_s,
        )
        stt = ElevenLabsSTT(
            settings.elevenlabs_api_key,
            settings.elevenlabs_base_url,
            settings.stt_realtime_model,
            settings.stt_batch_model,
            settings.stt_language,
            settings.stt_vad_silence_s,
            settings.stt_keyterms,
            http,
        )
        tts = ElevenLabsTTS(
            settings.elevenlabs_api_key,
            settings.elevenlabs_base_url,
            settings.tts_voice_id,
            http,
            max_concurrent=settings.tts_max_concurrent,
        )

    store = build_store(settings.redis_url)
    bus: Bus = RedisBus(settings.redis_url) if settings.redis_url else MemoryBus()
    agent: AgentClient | None = None
    if settings.agent_configured:
        if settings.agent_service_url:
            agent = HTTPAgentClient(settings.agent_service_url, settings.agent_token, http)
        else:
            agent = InProcessAgentClient(provider, mock=settings.mock_providers, bus=bus)

    watcher: Poller | NoopWatcher
    if settings.sunco_delivery == "poll" or settings.mock_providers:
        watcher = Poller(provider, bus, settings.sunco_poll_interval_s)
    else:
        watcher = NoopWatcher()
    return Services(
        settings=settings,
        provider=provider,
        stt=stt,
        tts=tts,
        bus=bus,
        watcher=watcher,
        cache=TTSCache(settings.tts_cache_items, settings.tts_cache_ttl_s, settings.redis_url),
        http=http,
        agent=agent,
        store=store,
        limits=Limits(
            store,
            enabled=settings.limits_enabled,
            per_minute=settings.limit_per_minute,
            max_calls=settings.limit_concurrent_calls,
            daily_voice_s=settings.limit_daily_voice_minutes * 60,
        ),
        skip_patterns=[re.compile(p, re.IGNORECASE) for p in settings.speech_skip_patterns],
    )
