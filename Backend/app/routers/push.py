"""Web Push subscriptions: the devices a signed-in user allowed Grace to notify."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from app import main
from app.config import settings
from app.database import PushSubscription
from app.services import web_push

router = APIRouter(prefix="/api/v1", tags=["push"])

MAX_DEVICES = 10


class PushKeys(BaseModel):
    p256dh: str = Field(min_length=20, max_length=200)
    auth: str = Field(min_length=8, max_length=100)


class PushSubscriptionIn(BaseModel):
    endpoint: str = Field(min_length=10, max_length=1000)
    keys: PushKeys


class PushSubscriptionOut(BaseModel):
    endpoint: str = Field(min_length=10, max_length=1000)


def push_key():
    """The server's public VAPID key, or null when push is not set up on this server."""
    return {"public_key": settings.vapid_public_key if web_push.enabled() else None}


def subscribe(payload: PushSubscriptionIn, u=Depends(main.user_dep)):
    if not web_push.enabled():
        raise HTTPException(503, "Notifications to devices are not set up on this server.") from None
    if not web_push.allowed_endpoint(payload.endpoint):
        raise HTTPException(422, "That is not a browser push service endpoint.") from None
    with main.SessionLocal() as db:
        existing = db.scalar(select(PushSubscription).where(PushSubscription.endpoint == payload.endpoint))
        if existing is not None:
            # The same browser again (or after switching accounts): it belongs to this user now.
            existing.user_id, existing.p256dh, existing.auth = (u.id, payload.keys.p256dh, payload.keys.auth)
        else:
            count = db.scalar(select(func.count(PushSubscription.id)).where(PushSubscription.user_id == u.id))
            if (count or 0) >= MAX_DEVICES:
                raise HTTPException(409, f"Notifications can go to up to {MAX_DEVICES} devices.") from None
            db.add(
                PushSubscription(
                    user_id=u.id,
                    endpoint=payload.endpoint,
                    p256dh=payload.keys.p256dh,
                    auth=payload.keys.auth,
                )
            )
        db.commit()
        devices = db.scalar(select(func.count(PushSubscription.id)).where(PushSubscription.user_id == u.id))
    return {"subscribed": True, "devices": devices}


def unsubscribe(payload: PushSubscriptionOut, u=Depends(main.user_dep)):
    with main.SessionLocal() as db:
        row = db.scalar(
            select(PushSubscription).where(
                PushSubscription.endpoint == payload.endpoint, PushSubscription.user_id == u.id
            )
        )
        if row is not None:
            db.delete(row)
            db.commit()
    return {"subscribed": False}


router.add_api_route("/push/key", push_key, methods=["GET"])
router.add_api_route("/push/subscriptions", subscribe, methods=["POST"])
router.add_api_route("/push/subscriptions/remove", unsubscribe, methods=["POST"])
