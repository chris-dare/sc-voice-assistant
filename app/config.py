"""Runtime configuration, read from environment variables (and an optional .env file)."""

from __future__ import annotations

import secrets
from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


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
    stt_keyterms: list[str] = Field(
        default_factory=lambda: ["data bundle", "airtime", "MoMo", "roaming", "USSD", "SIM", "PUK"]
    )
    tts_voice_id: str = "JBFqnCBsd6RMkjVDRZzb"
    tts_model_voice: str = "eleven_flash_v2_5"  # lowest latency, voice mode
    tts_model_readaloud: str = "eleven_multilingual_v2"  # higher quality, read-aloud
    tts_sample_rate: int = 24000  # PCM for voice mode
    tts_readaloud_format: str = "mp3_44100_128"
    tts_max_chars: int = 2000

    # --- Gateway ---------------------------------------------------------------------
    auth_secret: str = Field(default_factory=lambda: secrets.token_urlsafe(32))
    auth_token_ttl_s: int = 7 * 24 * 3600
    redis_url: str | None = None
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

    @property
    def sunco_configured(self) -> bool:
        return bool(self.zendesk_app_id and self.zendesk_key_id and self.zendesk_secret)

    @property
    def elevenlabs_configured(self) -> bool:
        return bool(self.elevenlabs_api_key)


@lru_cache
def get_settings() -> Settings:
    return Settings()
