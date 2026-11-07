"""POC user tokens.

The mobile app will bring its own user token (open question 4 in the design). Until then the
gateway issues a signed token for an anonymous demo user. The SunCo external ID is always
derived from the verified token on the server, never taken from the client.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import time
import uuid

_USER_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")


class AuthError(Exception):
    pass


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _sign(secret: str, payload: str) -> str:
    return _b64(hmac.new(secret.encode(), payload.encode(), hashlib.sha256).digest())


def new_user_id() -> str:
    return uuid.uuid4().hex


def issue_token(secret: str, user_id: str, ttl_s: int, now: float | None = None) -> str:
    if not _USER_ID_RE.match(user_id):
        raise AuthError("invalid user id")
    exp = int((now or time.time()) + ttl_s)
    payload = f"{user_id}.{exp}"
    return f"{payload}.{_sign(secret, payload)}"


def verify_token(secret: str, token: str, now: float | None = None) -> str:
    try:
        user_id, exp_s, sig = token.split(".")
        exp = int(exp_s)
    except ValueError as exc:
        raise AuthError("malformed token") from exc
    if not hmac.compare_digest(sig, _sign(secret, f"{user_id}.{exp}")):
        raise AuthError("bad signature")
    if exp < (now or time.time()):
        raise AuthError("token expired")
    if not _USER_ID_RE.match(user_id):
        raise AuthError("invalid user id")
    return user_id


def external_id_for(user_id: str) -> str:
    return f"voicepoc-{user_id}"
