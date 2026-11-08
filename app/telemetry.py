"""Logs, traces and turn metrics to Pydantic Logfire when LOGFIRE_TOKEN is set (7.3 Operations).

Transcript and reply text are never logged; spans carry IDs and timings only (6.2).
"""

from __future__ import annotations

import logging
import os

import logfire
from fastapi import FastAPI

_configured = False


def setup(app: FastAPI, service_version: str, environment: str, log_level: str) -> None:
    global _configured
    if not _configured:
        # LOGFIRE_TOKEN is Logfire's standard name; the environment stores the key as LOGFIRE_API_KEY.
        token = os.environ.get("LOGFIRE_TOKEN") or os.environ.get("LOGFIRE_API_KEY") or None
        logfire.configure(
            token=token,
            service_name="sc-voice-gateway",
            service_version=service_version,
            environment=environment,
            send_to_logfire="if-token-present",
            console=False,
        )
        logfire.instrument_httpx(capture_headers=False)
        # Route stdlib logging (ours, uvicorn's) to Logfire as well as the console.
        logging.basicConfig(
            level=log_level,
            format="%(asctime)s %(levelname)s %(name)s: %(message)s",
            handlers=[logging.StreamHandler(), logfire.LogfireLoggingHandler()],
            force=True,
        )
        _configured = True
    logfire.instrument_fastapi(app, excluded_urls="/healthz")


def turn_metrics(metrics: dict[str, object]) -> None:
    logfire.info(
        "voice turn {turn_id}: total {total_ms} ms, bot {bot_ms} ms",
        **{k: v for k, v in metrics.items() if k != "type"},
    )
