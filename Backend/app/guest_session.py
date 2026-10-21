"""The guest session cookie: the one place that writes and reads it.

In production the cookie value is ``<uuid>.<base64 HMAC>`` so a guest cannot
forge another guest's id; in development it is the bare uuid. Every reader
must go through :func:`read_guest_session_id` — parsing the raw cookie with
``uuid.UUID(...)`` works in development and silently breaks in production
(it broke email verification, login adoption and reopening guest chats).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import uuid

from fastapi import Request, Response

from app.config import settings

GUEST_COOKIE = "guest_session_id"
GUEST_SESSION_TTL_SECONDS = 24 * 60 * 60


def _signature(session_id: str) -> bytes:
    secret = (settings.csrf_key or settings.jwt_secret).encode()
    return hmac.new(secret, session_id.encode(), hashlib.sha256).digest()


def guest_cookie_value(session_id: str) -> str:
    if not settings.is_production:
        return session_id
    return f"{session_id}.{base64.urlsafe_b64encode(_signature(session_id)).decode()}"


def parse_guest_cookie(value: str | None) -> str | None:
    """The verified guest session id, or None if absent, malformed or forged."""
    if not value:
        return None
    parts = value.split(".", 1)
    try:
        session_id = str(uuid.UUID(parts[0]))
    except (ValueError, AttributeError):
        return None
    if not settings.is_production:
        return session_id
    if len(parts) != 2:
        return None
    try:
        supplied = base64.urlsafe_b64decode(parts[1].encode())
    except (ValueError, TypeError):
        return None
    return session_id if hmac.compare_digest(supplied, _signature(session_id)) else None


def read_guest_session_id(request: Request) -> str | None:
    return parse_guest_cookie(request.cookies.get(GUEST_COOKIE))


def set_guest_cookie(response: Response, session_id: str) -> None:
    response.set_cookie(
        key=GUEST_COOKIE,
        value=guest_cookie_value(session_id),
        httponly=True,
        secure=settings.is_production,
        samesite="lax",
        max_age=GUEST_SESSION_TTL_SECONDS,
        path="/",
    )
