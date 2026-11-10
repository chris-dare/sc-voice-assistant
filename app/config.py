"""Runtime configuration, read from environment variables (and an optional .env file)."""

from __future__ import annotations

import secrets
from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from zigi_agent.lexicon import KEYTERMS


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # --- Sunshine Conversations (SunCo) ---------------------------------------------
    zendesk_app_id: str = ""
    zendesk_key_id: str = ""
    zendesk_secret: str = ""
    # US: https://api.smooch.io · EU: https://api.eu-1.smooch.io
    # Zendesk-hosted: https://<subdomain>.zendesk.com/sc
    sunco_base_url: str = "https://api.smooch.io"
    # "poll" works without a public URL (local dev). "webhook" needs SunCo to reach /webhooks/sunco.
    sunco_delivery: Literal["poll", "webhook"] = "poll"
    sunco_poll_interval_s: float = 0.5
    sunco_webhook_secret: str = ""
    sunco_timeout_s: float = 10.0

    # --- ElevenLabs ------------------------------------------------------------------
    elevenlabs_api_key: str = ""
    elevenlabs_base_url: str = "https://api.elevenlabs.io"
    stt_mode: Literal["realtime", "batch"] = "realtime"
    stt_realtime_model: str = "scribe_v2_realtime"
    stt_batch_model: str = "scribe_v2"
    stt_language: str | None = "en"
    stt_vad_silence_s: float = 0.6
    # Telco vocabulary that helps STT spell plan and product names right.
    # Place and product names the transcriber otherwise mishears ("North Ligon", "Faba"); see zigi_agent/lexicon.py
    stt_keyterms: list[str] = Field(default_factory=lambda: list(KEYTERMS))
    tts_voice_id: str = "NWCYzEZIRjKG9C8z4tSK"  # Leonard: young Ghanaian male, conversational (chosen 10 Nov; was Alex Oke)
    tts_model_voice: str = "eleven_flash_v2_5"  # lowest latency, voice mode
    tts_model_readaloud: str = "eleven_multilingual_v2"  # higher quality, read-aloud
    tts_sample_rate: int = 24000  # PCM for voice mode
    tts_readaloud_format: str = "mp3_44100_128"
    tts_max_chars: int = 2000
    tts_max_concurrent: int = 2  # per instance; ElevenLabs plan allows 3 in total

    # --- Gateway ---------------------------------------------------------------------
    auth_secret: str = Field(default_factory=lambda: secrets.token_urlsafe(32))
    auth_token_ttl_s: int = 7 * 24 * 3600
    redis_url: str | None = None
    # Usage limits per user (design 5.7). They protect vendor capacity (ElevenLabs, OpenRouter) from one client.
    limits_enabled: bool = True
    limit_per_minute: dict[str, int] = Field(
        default_factory=lambda: {"session": 20, "messages": 30, "attachments": 10, "voice_notes": 10, "read_aloud": 30, "voice_turns": 30}
    )
    limit_concurrent_calls: int = 2  # a reconnecting call briefly holds two slots
    limit_daily_voice_minutes: int = 60
    call_state_ttl_s: int = 1800  # agentic call state kept this long after the last turn
    turn_quiet_window_s: float = 1.5
    # The dev bot can take 20 s or more on FAQ answers (measured 07 Nov 2026).
    bot_timeout_s: float = 45.0
    # Spoken once per turn when the bot has not answered yet. Empty string disables it.
    hold_prompt: str = "One moment while I check that for you."
    hold_after_s: float = 2.5
    # Bot lines that are shown on screen but never spoken (the dev bot emits debug lines).
    speech_skip_patterns: list[str] = Field(default_factory=lambda: [r"^zigi 2 preprod$", r"^PRODUCTION$"])
    session_max_s: int = 900
    idle_timeout_s: int = 120
    tts_cache_items: int = 256
    tts_cache_ttl_s: int = 24 * 3600
    # Fake SunCo bot, STT and TTS: lets the demo run with no network or keys.
    mock_providers: bool = False
    log_level: str = "INFO"
    environment: str = "dev"
    sse_keepalive_s: float = 15.0
    # Vercel caps a request body at 4.5 MB; the client downsizes photos well below this.
    attachment_max_bytes: int = 4_000_000
    # PostHog product analytics (client side). The project token is public by design.
    posthog_project_token: str = ""
    posthog_region: str = "EU Cloud"

    @property
    def posthog_api_host(self) -> str:
        return "https://eu.i.posthog.com" if "eu" in self.posthog_region.lower() else "https://us.i.posthog.com"

    # --- Agentic mode (design 5.8): the Zigi agent service, Zigi as its tool ------------------
    # Feature flag. The agent service has its own settings (zigi_agent/config.py).
    agent_mode_enabled: bool = False
    # The separate agent service; empty runs the same service code in this process (local dev, tests).
    agent_service_url: str = ""
    agent_service_token: str = ""
    # The LLM credential, used by the in-process agent only (the service reads its own).
    openrouter_api_key: str = ""
    # Wait this long for the agent's first event (it has its own model fallback inside) before relaying the turn.
    agent_first_event_timeout_s: float = 8.0
    agent_quiet_s: float = 1.2  # a Zigi message burst that arrives on its own is complete after this much silence
    # Wait this long for the rest of a phone number or code the caller paused in the middle of
    agent_dictation_wait_s: float = 3.0
    voice_mode_default: Literal["relay", "agent"] = "relay"
    # Client sends silence instead of mic audio while its VAD hears no speech (background noise stays out of STT).
    noise_gate: bool = True
    # Client learns the caller's speaking level and ignores voices well below it (others in the room)
    voice_focus: bool = True
    # Testing only: log what the caller and the agent said (off in normal use; design 6.2 keeps text out of logs)
    log_conversation_text: bool = False
    # Commit the transcript this long after the client's end of speech if STT has not (noise can hold STT's VAD open).
    stt_force_commit_s: float = 1.0

    @property
    def agent_configured(self) -> bool:
        return self.agent_mode_enabled and bool(self.agent_service_url or self.openrouter_api_key or self.mock_providers)

    @property
    def agent_token(self) -> str:
        """Bearer token for the agent service: AGENT_SERVICE_TOKEN, else derived from AUTH_SECRET as the service does."""
        import hashlib
        import hmac

        if self.agent_service_token:
            return self.agent_service_token
        return hmac.new(self.auth_secret.encode(), b"zigi-agent-service", hashlib.sha256).hexdigest()

    # When set, /v1/session asks for this code (protects vendor credit on a public demo URL).
    demo_access_code: str = ""

    @property
    def sunco_configured(self) -> bool:
        return bool(self.zendesk_app_id and self.zendesk_key_id and self.zendesk_secret)

    @property
    def elevenlabs_configured(self) -> bool:
        return bool(self.elevenlabs_api_key)


@lru_cache
def get_settings() -> Settings:
    return Settings()
