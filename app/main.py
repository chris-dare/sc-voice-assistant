"""Voice Gateway HTTP and WebSocket API."""

import asyncio
import contextlib
import hmac
import json
import logging
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Annotated, Any

import logfire
from fastapi import (
    Depends,
    FastAPI,
    File,
    Form,
    Header,
    HTTPException,
    Query,
    Request,
    UploadFile,
    WebSocket,
)
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import telemetry
from .auth import AuthError, issue_token, new_user_id, verify_token
from .bus import channel_for
from .config import Settings, get_settings
from .delivery import handle_webhook, webhook_secret_ok
from .services import Services, UserContext, build_services
from .session import VoiceSession
from .speech import render
from .stt import STTError
from .sunco import SunCoError, media_urls
from .tts import TTSError, content_type_for

log = logging.getLogger("app")
WEB_DIR = Path(__file__).resolve().parent.parent / "web"
MAX_VOICE_NOTE_BYTES = 10 * 1024 * 1024
_ALLOWED_FILE_TYPES = {
    "application/pdf",
    "text/plain",
    "text/csv",
    "application/msword",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.ms-excel",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.ms-powerpoint",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
}
_EXT = {"image/jpeg": "jpg", "image/png": "png", "image/gif": "gif", "image/webp": "webp", "image/heic": "heic"}


def allowed_attachment_type(ctype: str) -> bool:
    return ctype in _ALLOWED_FILE_TYPES or (ctype.startswith("image/") and ctype != "image/svg+xml")


def safe_filename(name: str | None, ctype: str) -> str:
    """Keep a readable base name; drop paths and characters that break headers or URLs."""
    import re

    base = (name or "").replace("\\", "/").split("/")[-1].strip()
    base = re.sub(r"[^A-Za-z0-9._ -]", "_", base)[:100].strip(" .")
    return base or f"attachment.{_EXT.get(ctype, 'bin')}"


def create_app(settings: Settings | None = None, services: Services | None = None) -> FastAPI:
    settings = settings or get_settings()

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        get_svc(app)
        yield
        await app.state.svc.aclose()

    app = FastAPI(title="SC Voice Gateway", version="0.1.0", lifespan=lifespan)
    telemetry.setup(app, app.version, settings.environment, settings.log_level)

    def get_svc(a: FastAPI) -> Services:
        # Built lazily too, for platforms that skip the ASGI lifespan (serverless).
        if getattr(a.state, "svc", None) is None:
            a.state.svc = services or build_services(settings)
        return a.state.svc

    def svc(request: Request) -> Services:
        return get_svc(request.app)

    def bearer(authorization: str | None, token: str | None) -> str:
        if authorization and authorization.lower().startswith("bearer "):
            return authorization[7:]
        if token:
            return token
        raise HTTPException(401, "missing token")

    async def current_user(
        request: Request,
        authorization: Annotated[str | None, Header()] = None,
        token: Annotated[str | None, Query()] = None,
    ) -> UserContext:
        s = svc(request)
        try:
            user_id = verify_token(s.settings.auth_secret, bearer(authorization, token))
        except AuthError as exc:
            raise HTTPException(401, str(exc)) from exc
        try:
            return await s.resolve(user_id)
        except SunCoError as exc:
            raise HTTPException(502, f"conversation service unavailable: {exc}") from exc

    User = Annotated[UserContext, Depends(current_user)]

    # ------------------------------------------------------------------ session
    class SessionIn(BaseModel):
        user_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{8,64}$")
        access_code: str | None = Field(default=None, max_length=200)

    @app.post("/v1/session")
    async def create_session(body: SessionIn, request: Request) -> dict[str, Any]:
        """POC sign-in: returns a signed token for a demo user (the mobile app will bring its own)."""
        s = svc(request)
        code = s.settings.demo_access_code
        if code and not hmac.compare_digest(code.encode(), (body.access_code or "").encode()):
            raise HTTPException(403, "access code required")
        user_id = body.user_id or new_user_id()
        return {"token": issue_token(s.settings.auth_secret, user_id, s.settings.auth_token_ttl_s), "user_id": user_id}

    @app.get("/v1/config")
    async def client_config(request: Request) -> dict[str, Any]:
        s = svc(request).settings
        return {
            "tts_sample_rate": s.tts_sample_rate,
            "stt_sample_rate": 16000,
            "mock": s.mock_providers,
            "access_code_required": bool(s.demo_access_code),
            "attachment_max_bytes": s.attachment_max_bytes,
            "posthog": {"token": s.posthog_project_token, "api_host": s.posthog_api_host} if s.posthog_project_token else None,
            "voice_modes": ["relay", "agent"] if s.agent_configured else ["relay"],
            "voice_mode_default": s.voice_mode_default if s.agent_configured else "relay",
            "noise_gate": s.noise_gate,
        }

    # ------------------------------------------------------------------ text mode
    @app.get("/v1/conversations/me/messages")
    async def history(user: User, request: Request) -> dict[str, Any]:
        s = svc(request)
        try:
            msgs = await s.provider.list_messages(user.conversation_id)
        except SunCoError as exc:
            raise HTTPException(502, str(exc)) from exc
        return {"conversation_id": user.conversation_id, "messages": [m.to_dict() for m in msgs]}

    @app.get("/v1/conversations/me/stream")
    async def stream(user: User, request: Request) -> StreamingResponse:
        """Server-sent events with each new bot message, for text mode."""
        s = svc(request)

        async def events() -> AsyncIterator[bytes]:
            yield b": connected\n\n"
            async with s.bus.subscribe(channel_for(user.conversation_id)) as sub, s.watcher.watch(user.conversation_id):
                # Pump the subscription into a queue: a timeout must cancel queue.get(), never the
                # subscription's async generator (cancelling __anext__ closes it for good).
                queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

                async def pump() -> None:
                    async for ev in sub:
                        queue.put_nowait(ev)

                pump_task = asyncio.create_task(pump())
                try:
                    while True:
                        if await request.is_disconnected():
                            return
                        try:
                            event = await asyncio.wait_for(queue.get(), timeout=s.settings.sse_keepalive_s)
                        except TimeoutError:
                            yield b": keep-alive\n\n"
                            continue
                        if event.get("kind") == "bot_message":
                            yield f"event: bot_message\ndata: {json.dumps(event['message'])}\n\n".encode()
                finally:
                    pump_task.cancel()

        return StreamingResponse(events(), media_type="text/event-stream", headers={"cache-control": "no-cache", "x-accel-buffering": "no"})

    class TextIn(BaseModel):
        text: str = Field(min_length=1, max_length=2000)
        input: str = Field(default="typed", pattern="^(typed|voice_note)$")

    @app.post("/v1/messages")
    async def send_text(body: TextIn, user: User, request: Request) -> dict[str, Any]:
        s = svc(request)
        try:
            msg = await s.provider.post_text(
                user.conversation_id,
                user.sunco_user_id,
                body.text.strip(),
                {"channel": "text", "source": "sc-voice-gateway", "input": body.input},
            )
        except SunCoError as exc:
            raise HTTPException(502, str(exc)) from exc
        return {"message": msg.to_dict()}

    @app.post("/v1/attachments")
    async def send_attachment(
        user: User,
        request: Request,
        file: Annotated[UploadFile, File()],
        text: Annotated[str | None, Form(max_length=2000)] = None,
    ) -> dict[str, Any]:
        """Upload a photo or file to SunCo and post it as an image or file message from the user."""
        s = svc(request)
        limit = s.settings.attachment_max_bytes
        data = await file.read(limit + 1)
        if len(data) > limit:
            raise HTTPException(413, f"attachment is larger than {limit // 1_000_000} MB")
        if not data:
            raise HTTPException(400, "attachment is empty")
        ctype = (file.content_type or "application/octet-stream").split(";")[0].strip().lower()
        if not allowed_attachment_type(ctype):
            raise HTTPException(415, f"{ctype} files are not supported")
        name = safe_filename(file.filename, ctype)
        try:
            att = await s.provider.upload_attachment(user.conversation_id, name, data, ctype)
            content: dict[str, Any] = {
                "type": "image" if ctype.startswith("image/") else "file",
                "mediaUrl": att["mediaUrl"],
                "mediaType": att.get("mediaType") or ctype,
                "mediaSize": len(data),
                "altText": name[:128],
            }
            if text and text.strip():
                content["text"] = text.strip()
            msg = await s.provider.post_content(
                user.conversation_id,
                user.sunco_user_id,
                content,
                {"channel": "text", "source": "sc-voice-gateway", "input": "attachment"},
            )
        except SunCoError as exc:
            if exc.status in (400, 413, 415, 422):
                raise HTTPException(422, "The messaging service rejected this file.") from exc
            raise HTTPException(502, str(exc)) from exc
        return {"message": msg.to_dict()}

    @app.get("/v1/media")
    async def media(url: str, user: User, request: Request) -> Response:
        """Serve an attachment from the caller's conversation. Attachments are private in SunCo, so the
        gateway fetches a short-lived signed URL with the app key and redirects the browser to it."""
        s = svc(request)
        try:
            msgs = await s.provider.list_messages(user.conversation_id)
        except SunCoError as exc:
            raise HTTPException(502, str(exc)) from exc
        if not any(url in media_urls(m.content) for m in msgs):
            raise HTTPException(404, "attachment not found")
        try:
            target = await s.provider.resolve_media(url)
        except SunCoError as exc:
            raise HTTPException(404 if exc.status in (400, 401, 403, 404) else 502, "attachment unavailable") from exc
        headers = {"cache-control": "private, max-age=60"}
        if target.redirect:
            return RedirectResponse(target.redirect, status_code=302, headers=headers)
        return Response(target.data or b"", media_type=target.content_type, headers=headers)

    @app.post("/v1/voice-notes")
    async def voice_note(user: User, request: Request, file: Annotated[UploadFile, File()]) -> dict[str, Any]:
        """Flow 2: transcribe a voice note. The client shows the text for review before sending."""
        s = svc(request)
        audio = await file.read(MAX_VOICE_NOTE_BYTES + 1)
        if len(audio) > MAX_VOICE_NOTE_BYTES:
            raise HTTPException(413, "voice note too large")
        if len(audio) < 1000:
            raise HTTPException(400, "voice note is empty")
        t0 = time.monotonic()
        try:
            text = await s.stt.transcribe(audio, file.filename or "note.webm", file.content_type or "audio/webm")
        except STTError as exc:
            raise HTTPException(502, str(exc)) from exc
        return {"transcript": text, "stt_ms": round((time.monotonic() - t0) * 1000)}

    @app.get("/v1/messages/{message_id}/audio")
    async def read_aloud(message_id: str, user: User, request: Request) -> StreamingResponse:
        """Flow 3: read a bot message aloud. The message must be in the caller's conversation."""
        s = svc(request)
        cfg = s.settings
        key = f"{cfg.tts_voice_id}:{cfg.tts_model_readaloud}:{cfg.tts_readaloud_format}:{message_id}"
        if hit := await s.cache.get(key):
            audio, ctype = hit
            return StreamingResponse(iter([audio]), media_type=ctype, headers={"x-cache": "hit"})
        try:
            msg = await s.provider.get_message(user.conversation_id, message_id)
        except SunCoError as exc:
            raise HTTPException(404 if exc.status in (400, 404) else 502, "message not found") from exc
        speech = render(msg.content)[: cfg.tts_max_chars]
        if not speech:
            raise HTTPException(422, "nothing to read aloud")
        fmt = cfg.tts_readaloud_format if not cfg.mock_providers else "wav"
        ctype = content_type_for(fmt) if fmt != "wav" else "audio/wav"
        stream_iter = s.tts.stream(speech, cfg.tts_model_readaloud, fmt).__aiter__()
        try:
            first = await stream_iter.__anext__()
        except (TTSError, StopAsyncIteration) as exc:
            raise HTTPException(502, f"text-to-speech failed: {exc}") from exc

        async def body() -> AsyncIterator[bytes]:
            chunks = [first]
            yield first
            async for chunk in stream_iter:
                chunks.append(chunk)
                yield chunk
            await s.cache.put(key, b"".join(chunks), ctype)

        return StreamingResponse(body(), media_type=ctype, headers={"x-cache": "miss"})

    # ------------------------------------------------------------------ voice mode
    @app.websocket("/v1/voice/session")
    async def voice(ws: WebSocket, token: str = "", mode: str = "") -> None:
        s: Services = get_svc(ws.app)
        try:
            user_id = verify_token(s.settings.auth_secret, token)
        except AuthError:
            await ws.close(code=4401, reason="unauthorized")
            return
        await ws.accept()
        try:
            ctx = await s.resolve(user_id)
        except SunCoError:
            await ws.send_text(json.dumps({"type": "error", "code": "unavailable", "text": "Conversation service unavailable."}))
            await ws.close(code=1011)
            return
        session = VoiceSession(ws, s, ctx, mode or s.settings.voice_mode_default)
        with logfire.span("voice session {conversation_id}", conversation_id=ctx.conversation_id, mode=session.mode):
            await session.run()
        with contextlib.suppress(Exception):
            await ws.close()

    # ------------------------------------------------------------------ webhook
    @app.post("/webhooks/sunco")
    async def sunco_webhook(request: Request, x_api_key: Annotated[str | None, Header()] = None) -> JSONResponse:
        s = svc(request)
        if not webhook_secret_ok(s.settings.sunco_webhook_secret, x_api_key):
            raise HTTPException(401, "bad webhook secret")
        try:
            body = await request.json()
        except ValueError as exc:
            raise HTTPException(400, "invalid JSON") from exc
        published = await handle_webhook(s.bus, s.webhook_seen, body)
        return JSONResponse({"published": published})

    # ------------------------------------------------------------------ misc
    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    if WEB_DIR.exists():
        app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")

        @app.get("/", include_in_schema=False)
        async def index() -> FileResponse:
            return FileResponse(WEB_DIR / "index.html")

    return app


app = create_app()
