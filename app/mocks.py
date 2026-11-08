"""Fake SunCo bot, STT and TTS for tests and offline demos (MOCK_PROVIDERS=true)."""

from __future__ import annotations

import asyncio
import itertools
import math
import re
import struct
import time
import uuid
from collections import defaultdict
from collections.abc import AsyncIterator
from typing import Any

from .stt import TranscriptEvent
from .sunco import Message

_FAQ = {
    "roaming": "Roaming lets you use your MTN number abroad. Prepaid roaming is on by default. Dial *135# to check rates.",
    "bundle": "You can buy a data bundle by dialling *138#. For example, 5GB costs GH₵25.",
    "momo": "MoMo is MTN Mobile Money. Dial *170# to send money, pay bills or buy airtime.",
}


class MockSunCo:
    """In-memory conversations with a scripted telco FAQ bot."""

    def __init__(self, reply_delay_s: float = 0.3) -> None:
        self._delay = reply_delay_s
        self._users: dict[str, str] = {}
        self._convs: dict[str, str] = {}
        self._messages: dict[str, list[Message]] = defaultdict(list)
        self._clock = itertools.count()
        self._media: dict[str, tuple[bytes, str]] = {}
        self._flow: dict[str, str] = {}  # conversation -> "otp" | "confirm": a strict, text-style purchase flow

    def _msg(self, conv: str, author: str, content: dict[str, Any], metadata: dict[str, Any] | None = None) -> Message:
        m = Message(
            id=uuid.uuid4().hex,
            conversation_id=conv,
            author_type=author,
            author_name="Zigi" if author == "business" else None,
            received=f"{time.time():.3f}-{next(self._clock):06d}",
            content=content,
            metadata=metadata or {},
        )
        self._messages[conv].append(m)
        return m

    async def ensure_user(self, external_id: str) -> str:
        return self._users.setdefault(external_id, uuid.uuid4().hex)

    async def ensure_conversation(self, user_id: str) -> str:
        return self._convs.setdefault(user_id, uuid.uuid4().hex)

    async def post_text(self, conversation_id: str, user_id: str, text: str, metadata: dict[str, Any] | None = None) -> Message:
        msg = self._msg(conversation_id, "user", {"type": "text", "text": text}, metadata)
        asyncio.get_running_loop().create_task(self._reply(conversation_id, text))
        return msg

    async def _reply(self, conv: str, text: str) -> None:
        await asyncio.sleep(self._delay)
        lowered = text.lower().strip()
        flow = self._flow.pop(conv, None)
        if flow == "otp":
            if lowered.isdigit() and 4 <= len(lowered) <= 8:
                self._flow[conv] = "confirm"
                self._msg(
                    conv,
                    "business",
                    {
                        "type": "text",
                        "text": "Confirm purchase of 1GB for GH₵5? Reply YES or NO.",
                        "actions": [{"type": "reply", "text": "Yes", "payload": "yes"}, {"type": "reply", "text": "No", "payload": "no"}],
                    },
                )
            else:
                self._flow[conv] = "otp"
                self._msg(conv, "business", {"type": "text", "text": "Invalid OTP. Please enter only the digits of the OTP."})
            return
        if flow == "fibre":
            self._msg(
                conv,
                "business",
                {"type": "text", "text": f"Good news! MTN Fibre Broadband is available in {text.strip()}. Plans start at GH₵299 a month."},
            )
            return
        if flow == "confirm":
            done = "Done! 1GB has been added to your account." if lowered in {"yes", "y"} else "Okay, I've cancelled that purchase."
            self._msg(conv, "business", {"type": "text", "text": done})
            return
        if re.search(r"\bfib(re|er)\b", lowered):
            self._flow[conv] = "fibre"
            self._msg(conv, "business", {"type": "text", "text": "Sure! Which area or town should I check fibre availability for?"})
            return
        if re.search(r"\b(i want to|i'd like to|please) buy\b|\bpurchase\b", lowered):
            self._flow[conv] = "otp"
            self._msg(conv, "business", {"type": "text", "text": "To buy 1GB for GH₵5, please enter the OTP we sent to your number."})
            return
        if lowered == "attachment":
            self._msg(conv, "business", {"type": "text", "text": "Thanks, I've received your attachment."})
            return
        answer = next((a for k, a in _FAQ.items() if k in lowered), None)
        if answer:
            self._msg(conv, "business", {"type": "text", "text": answer})
            await asyncio.sleep(self._delay)
            self._msg(
                conv,
                "business",
                {
                    "type": "text",
                    "text": "Anything else?",
                    "actions": [
                        {"type": "reply", "text": "Data bundles", "payload": "bundle"},
                        {"type": "reply", "text": "MoMo", "payload": "momo"},
                        {"type": "reply", "text": "Roaming", "payload": "roaming"},
                    ],
                },
            )
        else:
            self._msg(conv, "business", {"type": "text", "text": f"You said: {text}. Ask me about data bundles, MoMo or roaming."})

    async def list_messages(self, conversation_id: str) -> list[Message]:
        return list(self._messages[conversation_id][-100:])

    async def post_content(
        self, conversation_id: str, user_id: str, content: dict[str, Any], metadata: dict[str, Any] | None = None
    ) -> Message:
        msg = self._msg(conversation_id, "user", content, metadata)
        if content.get("type") in {"image", "file"}:
            asyncio.get_running_loop().create_task(self._reply(conversation_id, "attachment"))
        elif content.get("type") == "text":
            asyncio.get_running_loop().create_task(self._reply(conversation_id, str(content.get("payload") or content.get("text") or "")))
        return msg

    async def upload_attachment(self, conversation_id: str, filename: str, data: bytes, content_type: str) -> dict[str, Any]:
        media_id = uuid.uuid4().hex
        self._media[media_id] = (data, content_type)
        return {"mediaUrl": f"https://mock.zendesk.com/sc/attachments/v2/{media_id}/{filename}", "mediaType": content_type}

    async def resolve_media(self, media_url: str):
        from .sunco import MediaTarget, SunCoError

        media_id = media_url.rstrip("/").split("/")[-2]
        if media_id not in self._media:
            raise SunCoError("not found", 404)
        data, ctype = self._media[media_id]
        return MediaTarget(data=data, content_type=ctype)

    async def get_message(self, conversation_id: str, message_id: str) -> Message:
        for m in self._messages[conversation_id]:
            if m.id == message_id:
                return m
        from .sunco import SunCoError

        raise SunCoError("not found", 404)

    async def aclose(self) -> None:
        return None


class MockRealtime:
    """Emits a final transcript after it has received ~0.5 s of audio followed by a commit or 1 s of audio."""

    def __init__(self, phrase: str) -> None:
        self._phrase = phrase
        self._bytes = 0
        self._q: asyncio.Queue[TranscriptEvent | None] = asyncio.Queue()

    async def send_pcm(self, pcm: bytes) -> None:
        before = self._bytes
        self._bytes += len(pcm)
        if before < 16000 <= self._bytes:
            self._q.put_nowait(TranscriptEvent("partial", self._phrase.split()[0]))
        if before < 32000 <= self._bytes:
            self._q.put_nowait(TranscriptEvent("final", self._phrase))
            self._bytes = 0

    async def commit(self) -> None:
        if self._bytes >= 8000:
            self._q.put_nowait(TranscriptEvent("final", self._phrase))
        self._bytes = 0

    async def events(self) -> AsyncIterator[TranscriptEvent]:
        while (ev := await self._q.get()) is not None:
            yield ev

    async def close(self) -> None:
        self._q.put_nowait(None)


class MockSTT:
    def __init__(self, phrase: str = "How do I buy a data bundle?") -> None:
        self.phrase = phrase

    async def open_realtime(self) -> MockRealtime:
        return MockRealtime(self.phrase)

    async def transcribe(self, audio: bytes, filename: str, content_type: str) -> str:
        return self.phrase


class MockTTS:
    """A soft 440 Hz tone, 40 ms per character, capped at 3 s. PCM, or WAV for read-aloud."""

    async def stream(self, text: str, model_id: str, output_format: str) -> AsyncIterator[bytes]:
        rate = 24000
        if output_format.startswith("pcm_"):
            rate = int(output_format.split("_")[1])
        n = int(rate * min(3.0, 0.04 * max(1, len(text))))
        samples = b"".join(struct.pack("<h", int(3000 * math.sin(2 * math.pi * 440 * i / rate))) for i in range(n))
        if not output_format.startswith("pcm_"):
            from .stt import pcm16_to_wav

            samples = pcm16_to_wav(samples, rate)
        for i in range(0, len(samples), 4096):
            await asyncio.sleep(0)
            yield samples[i : i + 4096]
