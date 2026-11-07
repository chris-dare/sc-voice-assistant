"""STT Adapter: ElevenLabs Scribe realtime (voice mode) and batch (voice notes)."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlencode

import httpx
from websockets.asyncio.client import ClientConnection, connect

log = logging.getLogger(__name__)

SAMPLE_RATE = 16000


class STTError(Exception):
    pass


@dataclass(slots=True)
class TranscriptEvent:
    kind: str  # "partial" | "final" | "error"
    text: str = ""


class RealtimeSTT(Protocol):
    async def send_pcm(self, pcm: bytes) -> None: ...
    async def commit(self) -> None: ...
    def events(self) -> AsyncIterator[TranscriptEvent]: ...
    async def close(self) -> None: ...


class STTProvider(Protocol):
    async def open_realtime(self) -> RealtimeSTT: ...
    async def transcribe(self, audio: bytes, filename: str, content_type: str) -> str: ...


_ERROR_TYPES = {
    "error", "auth_error", "quota_exceeded", "transcriber_error", "rate_limited", "input_error",
    "invalid_request", "queue_overflow", "resource_exhausted", "session_time_limit_exceeded",
    "chunk_size_exceeded", "unaccepted_terms", "unaccepted_terms_error",
}


class ElevenLabsRealtime:
    def __init__(self, ws: ClientConnection) -> None:
        self._ws = ws
        self._closed = False

    async def send_pcm(self, pcm: bytes) -> None:
        if self._closed:
            return
        await self._ws.send(
            json.dumps(
                {
                    "message_type": "input_audio_chunk",
                    "audio_base_64": base64.b64encode(pcm).decode(),
                    "commit": False,
                    "sample_rate": SAMPLE_RATE,
                }
            )
        )

    async def commit(self) -> None:
        if self._closed:
            return
        await self._ws.send(
            json.dumps({"message_type": "input_audio_chunk", "audio_base_64": "", "commit": True, "sample_rate": SAMPLE_RATE})
        )

    async def events(self) -> AsyncIterator[TranscriptEvent]:
        try:
            async for raw in self._ws:
                try:
                    data = json.loads(raw)
                except ValueError:
                    continue
                mtype = data.get("message_type", "")
                if mtype == "partial_transcript":
                    yield TranscriptEvent("partial", data.get("text", ""))
                elif mtype == "committed_transcript":
                    yield TranscriptEvent("final", data.get("text", ""))
                elif mtype in _ERROR_TYPES:
                    yield TranscriptEvent("error", data.get("error") or data.get("message") or mtype)
        except Exception as exc:  # connection dropped
            if not self._closed:
                yield TranscriptEvent("error", f"stt connection closed: {exc}")

    async def close(self) -> None:
        self._closed = True
        with contextlib.suppress(Exception):
            await self._ws.close()


class ElevenLabsSTT:
    def __init__(
        self,
        api_key: str,
        base_url: str,
        realtime_model: str,
        batch_model: str,
        language: str | None,
        vad_silence_s: float,
        keyterms: list[str],
        http: httpx.AsyncClient,
    ) -> None:
        self._key = api_key
        self._base = base_url.rstrip("/")
        self._realtime_model = realtime_model
        self._batch_model = batch_model
        self._language = language
        self._vad_silence_s = vad_silence_s
        self._keyterms = keyterms
        self._http = http

    def realtime_url(self) -> str:
        params: list[tuple[str, str]] = [
            ("model_id", self._realtime_model),
            ("audio_format", "pcm_16000"),
            ("commit_strategy", "vad"),
            ("vad_silence_threshold_secs", str(self._vad_silence_s)),
        ]
        if self._language:
            params.append(("language_code", self._language))
        params += [("keyterms", term) for term in self._keyterms]
        ws_base = self._base.replace("https://", "wss://").replace("http://", "ws://")
        return f"{ws_base}/v1/speech-to-text/realtime?{urlencode(params)}"

    async def open_realtime(self) -> RealtimeSTT:
        try:
            ws = await asyncio.wait_for(
                connect(self.realtime_url(), additional_headers={"xi-api-key": self._key}, max_size=2**22),
                timeout=8,
            )
        except Exception as exc:
            raise STTError(f"realtime STT connect failed: {exc}") from exc
        return ElevenLabsRealtime(ws)

    async def transcribe(self, audio: bytes, filename: str, content_type: str) -> str:
        data = {"model_id": self._batch_model}
        if self._language:
            data["language_code"] = self._language
        try:
            resp = await self._http.post(
                f"{self._base}/v1/speech-to-text",
                headers={"xi-api-key": self._key},
                data=data,
                files={"file": (filename, audio, content_type)},
                timeout=60,
            )
        except httpx.HTTPError as exc:
            raise STTError(f"batch STT failed: {exc}") from exc
        if resp.status_code != 200:
            raise STTError(f"batch STT {resp.status_code}: {resp.text[:300]}")
        return (resp.json().get("text") or "").strip()


def pcm16_to_wav(pcm: bytes, sample_rate: int = SAMPLE_RATE) -> bytes:
    import struct

    header = b"RIFF" + struct.pack("<I", 36 + len(pcm)) + b"WAVEfmt "
    header += struct.pack("<IHHIIHH", 16, 1, 1, sample_rate, sample_rate * 2, 2, 16)
    return header + b"data" + struct.pack("<I", len(pcm)) + pcm
