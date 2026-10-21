"""Web Push: notifications on the user's devices, opt-in (Jarvis roadmap, tier 2).

The standard browser mechanism (RFC 8030/8291/8292): the browser gives an endpoint
and keys, the app signs each request with its VAPID key and encrypts the payload, so
the push service never sees the content. Only notifications the user would see in
the notification centre are pushed, each once, a few at a time, and only while the
user's notifications setting is on. An endpoint the browser has dropped (404/410)
is deleted. Off unless VAPID keys are configured.

Generate keys: ``python -m app.services.web_push --generate-keys``.
"""

from __future__ import annotations

import base64
import json
import os
import sys
import time
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.database import Notification, PushSubscription, UserPersonalizationSetting
from app.observability import logger

# Push services of the major browsers. Endpoints elsewhere are refused, so a stored
# endpoint can never make the server call an arbitrary URL.
_PUSH_HOSTS = (
    "fcm.googleapis.com",
    "updates.push.services.mozilla.com",
    "web.push.apple.com",
    ".notify.windows.com",
    ".push.apple.com",
)
_PER_USER_PER_RUN = 3
_MAX_AGE = timedelta(hours=24)  # older notifications stay in the app; no flood after a pause


def enabled() -> bool:
    return bool(settings.vapid_public_key and settings.vapid_private_key)


def allowed_endpoint(endpoint: str) -> bool:
    parts = urlsplit(endpoint or "")
    host = (parts.hostname or "").lower()
    return (
        parts.scheme == "https"
        and len(endpoint) <= 1000
        and any(
            host == allowed or (allowed.startswith(".") and host.endswith(allowed)) for allowed in _PUSH_HOSTS
        )
    )


def _b64decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _encrypt(payload: bytes, p256dh: str, auth: str) -> bytes:
    import http_ece
    from cryptography.hazmat.primitives.asymmetric import ec

    server_key = ec.generate_private_key(ec.SECP256R1())
    return http_ece.encrypt(
        payload,
        salt=os.urandom(16),
        private_key=server_key,
        dh=_b64decode(p256dh),
        auth_secret=_b64decode(auth),
        version="aes128gcm",
    )


def _vapid_headers(endpoint: str) -> dict[str, str]:
    from py_vapid import Vapid

    parts = urlsplit(endpoint)
    vapid = Vapid.from_string(settings.vapid_private_key)
    claims = {
        "aud": f"{parts.scheme}://{parts.netloc}",
        "exp": int(time.time()) + 12 * 60 * 60,
        "sub": settings.vapid_subject,
    }
    return dict(vapid.sign(claims))


def payload_for(notification: Notification) -> dict[str, Any]:
    link = notification.link_path or (
        f"/movie/{notification.movie_tmdb_id}" if notification.movie_tmdb_id else "/"
    )
    return {"title": notification.title[:120], "body": (notification.body or "")[:300], "url": link}


def send(subscription: PushSubscription, payload: dict[str, Any], *, client: httpx.Client) -> str:
    """ "sent", "gone" (the browser dropped this endpoint) or "failed"."""
    if not allowed_endpoint(subscription.endpoint):
        return "gone"
    try:
        body = _encrypt(json.dumps(payload).encode(), subscription.p256dh, subscription.auth)
        headers = {
            **_vapid_headers(subscription.endpoint),
            "TTL": str(24 * 60 * 60),
            "Content-Encoding": "aes128gcm",
            "Content-Type": "application/octet-stream",
            "Urgency": "normal",
        }
        response = client.post(subscription.endpoint, content=body, headers=headers)
    except (httpx.HTTPError, ValueError, TypeError) as exc:
        logger.warning("web_push_failed error=%s", type(exc).__name__)
        return "failed"
    if response.status_code in {404, 410}:
        return "gone"
    if response.status_code >= 400:
        logger.warning("web_push_rejected status=%s", response.status_code)
        return "failed"
    return "sent"


def push_due_notifications_once(
    session: Session, *, now: datetime | None = None, client: httpx.Client | None = None
) -> int:
    """Push each due, unread notification once to every device its user allowed."""
    if not enabled():
        return 0
    now = now or datetime.now(UTC)
    users = session.scalars(select(PushSubscription.user_id).distinct()).all()
    if not users:
        return 0
    owned = client is None
    client = client or httpx.Client(timeout=10.0)
    sent = 0
    try:
        for user_id in users:
            setting = session.scalar(
                select(UserPersonalizationSetting).where(UserPersonalizationSetting.user_id == user_id)
            )
            if setting is not None and not setting.reminders_enabled:
                continue
            due = session.scalars(
                select(Notification)
                .where(
                    Notification.user_id == user_id,
                    Notification.pushed_at.is_(None),
                    Notification.read_at.is_(None),
                    Notification.dismissed_at.is_(None),
                    Notification.due_at <= now,
                    Notification.due_at >= now - _MAX_AGE,
                )
                .order_by(Notification.due_at.asc())
                .limit(_PER_USER_PER_RUN)
            ).all()
            if not due:
                continue
            devices = list(
                session.scalars(select(PushSubscription).where(PushSubscription.user_id == user_id)).all()
            )
            for notification in due:
                for device in list(devices):
                    outcome = send(device, payload_for(notification), client=client)
                    if outcome == "gone":
                        devices.remove(device)
                        session.delete(device)
                    elif outcome == "sent":
                        device.last_sent_at = now
                        sent += 1
                notification.pushed_at = now
            session.commit()
    finally:
        if owned:
            client.close()
    return sent


def generate_keys() -> dict[str, str]:
    """A new VAPID key pair: the public key for browsers, the private key for the server."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    private = ec.generate_private_key(ec.SECP256R1())
    raw_private = private.private_numbers().private_value.to_bytes(32, "big")
    raw_public = private.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )
    encode = lambda value: base64.urlsafe_b64encode(value).decode().rstrip("=")  # noqa: E731
    return {"VAPID_PUBLIC_KEY": encode(raw_public), "VAPID_PRIVATE_KEY": encode(raw_private)}


if __name__ == "__main__":  # pragma: no cover - operator tool
    if "--generate-keys" in sys.argv:
        for name, value in generate_keys().items():
            sys.stdout.write(f"{name}={value}\n")
    else:
        sys.stdout.write("Usage: python -m app.services.web_push --generate-keys\n")
