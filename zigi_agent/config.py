"""Settings for the agent service, from environment variables."""

from __future__ import annotations

import hashlib
import hmac
from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class AgentSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # LLM (OPENROUTER_API_KEY is only the credential; the gateway's AGENT_MODE_ENABLED is the feature flag)
    openrouter_api_key: str = ""
    agent_model: str = "openai/gpt-6-luna"
    # Tried in order when the main model errors or gives no first token in time.
    agent_fallback_models: list[str] = Field(default_factory=lambda: ["openai/gpt-5.6-luna"])
    agent_max_tokens: int = 300
    agent_first_event_timeout_s: float = 3.0
    agent_history_messages: int = 24
    # Zigi through SunCo
    zendesk_app_id: str = ""
    zendesk_key_id: str = ""
    zendesk_secret: str = ""
    sunco_base_url: str = "https://api.smooch.io"
    sunco_timeout_s: float = 10.0
    zigi_poll_interval_s: float = 0.4
    zigi_quiet_s: float = 1.2  # Zigi's reply burst is complete after this much silence
    zigi_timeout_s: float = 45.0
    speech_skip_patterns: list[str] = Field(default_factory=lambda: [r"^zigi 2 preprod$", r"^PRODUCTION$"])
    # Zigi's greeting and privacy notice at the start of a conversation: kept, but not Zigi's answer, so the
    # agent keeps waiting for the real reply (seen 08 Nov: the answer follows the greeting by 3 to 15 s).
    zigi_preamble_patterns: list[str] = Field(
        default_factory=lambda: [r"my name is zigi", r"privacy policy", r"^(hello|hi)!?\s+how can i (assist|help) you( today)?\??$"]
    )
    # Pages the agent may read (web_fetch), exact host names
    agent_link_hosts: list[str] = Field(
        default_factory=lambda: ["mtn.com.gh", "www.mtn.com.gh", "shop.mtn.com.gh", "mtncustomeronlinerequest.mtn.com.gh"]
    )
    # Service auth: a bearer token; by default derived from the AUTH_SECRET the gateway also has.
    agent_service_token: str = ""
    auth_secret: str = ""
    # Shared with the gateway: Zigi's messages arrive on its bus, so the agent need not poll SunCo.
    redis_url: str | None = None
    # A small model reads each Zigi reply (what it waits for, its options, the stakes); rules are the fallback
    zigi_reader_enabled: bool = True
    zigi_reader_model: str = "openai/gpt-6-luna"
    zigi_reader_timeout_s: float = 4.0
    # Semantic cache for Zigi's general FAQ answers (zigi_agent/cache.py)
    semantic_cache_enabled: bool = True
    semantic_cache_ttl_s: int = 6 * 3600
    semantic_cache_threshold: float = 0.9
    embedding_model: str = "openai/text-embedding-3-small"
    mock_providers: bool = False
    environment: str = "dev"

    @property
    def service_token(self) -> str:
        if self.agent_service_token:
            return self.agent_service_token
        if not self.auth_secret:
            return ""
        return hmac.new(self.auth_secret.encode(), b"zigi-agent-service", hashlib.sha256).hexdigest()


@lru_cache
def get_settings() -> AgentSettings:
    return AgentSettings()
