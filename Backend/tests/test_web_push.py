"""Web Push (services/web_push.py, routers/push.py): opt-in, encrypted, once per notification."""

from __future__ import annotations

import base64
import json
import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from httpx import ASGITransport, AsyncClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.config import settings
from app.database import Base, Notification, PushSubscription, SessionLocal, User, UserPersonalizationSetting
from app.services import web_push

FCM = "https://fcm.googleapis.com/fcm/send/abc123"
NOW = datetime(2026, 10, 16, 9, 0, tzinfo=UTC)


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


@pytest.fixture
def keys(monkeypatch):
    pair = web_push.generate_keys()
    monkeypatch.setattr(settings, "vapid_public_key", pair["VAPID_PUBLIC_KEY"])
    monkeypatch.setattr(settings, "vapid_private_key", pair["VAPID_PRIVATE_KEY"])
    return pair


def browser():
    """What a browser hands over when it subscribes: its public key and an auth secret."""
    private = ec.generate_private_key(ec.SECP256R1())
    public = private.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )
    auth = b"0123456789abcdef"
    return private, _b64(public), _b64(auth), auth


def test_only_browser_push_services_over_https_are_accepted():
    assert web_push.allowed_endpoint(FCM)
    assert web_push.allowed_endpoint("https://wns2-par02p.notify.windows.com/w/?token=x")
    assert web_push.allowed_endpoint("https://web.push.apple.com/QGk")
    assert not web_push.allowed_endpoint("http://fcm.googleapis.com/fcm/send/abc")
    assert not web_push.allowed_endpoint("https://evil.example/fcm.googleapis.com")
    assert not web_push.allowed_endpoint("https://169.254.169.254/latest")
    assert not web_push.allowed_endpoint("https://notify.windows.com.evil.example/x")


def test_the_payload_is_encrypted_so_only_the_browser_can_read_it(keys):
    import http_ece

    private, p256dh, auth_b64, auth = browser()
    sealed = web_push._encrypt(b'{"title": "Your daily brief"}', p256dh, auth_b64)
    assert b"daily brief" not in sealed
    opened = http_ece.decrypt(sealed, private_key=private, auth_secret=auth, version="aes128gcm")
    assert json.loads(opened) == {"title": "Your daily brief"}
    headers = web_push._vapid_headers(FCM)
    assert (
        headers["Authorization"].startswith("vapid t=")
        and keys["VAPID_PUBLIC_KEY"] in headers["Authorization"]
    )


def _device(endpoint=FCM):
    _private, p256dh, auth_b64, _auth = browser()
    return PushSubscription(user_id=1, endpoint=endpoint, p256dh=p256dh, auth=auth_b64)


@pytest.mark.parametrize(
    ("status", "outcome"), [(201, "sent"), (410, "gone"), (404, "gone"), (500, "failed")]
)
def test_each_push_service_answer_has_an_outcome(keys, status, outcome):
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(status)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert web_push.send(_device(), {"title": "x"}, client=client) == outcome
    assert seen[0].headers["Content-Encoding"] == "aes128gcm" and seen[0].headers["TTL"]


def test_a_refused_endpoint_or_a_network_error_never_raises(keys):
    def broken(request):
        raise httpx.ConnectError("down")

    with httpx.Client(transport=httpx.MockTransport(broken)) as client:
        assert web_push.send(_device(), {"title": "x"}, client=client) == "failed"
        assert web_push.send(_device("https://evil.example/x"), {"title": "x"}, client=client) == "gone"


def _db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = Session(engine, expire_on_commit=False)
    session.add_all([User(username="a", password_hash="x"), User(username="b", password_hash="x")])
    session.flush()
    return session


def test_due_notifications_are_pushed_once_and_dropped_devices_are_removed(keys):
    db = _db()
    gone = _device("https://updates.push.services.mozilla.com/wpush/v2/gone")
    db.add_all([_device(), gone])
    db.add_all(
        [
            Notification(user_id=1, kind="reminder", title="Watch Arrival", movie_tmdb_id=329865, due_at=NOW),
            Notification(user_id=1, kind="release", title="Old", due_at=NOW - timedelta(days=3)),
            Notification(user_id=1, kind="reminder", title="Later", due_at=NOW + timedelta(hours=2)),
        ]
    )
    db.commit()
    posted = []

    def handler(request):
        posted.append(str(request.url))
        return httpx.Response(410 if "gone" in str(request.url) else 201)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert web_push.push_due_notifications_once(db, now=NOW, client=client) == 1
        assert web_push.push_due_notifications_once(db, now=NOW, client=client) == 0  # once
    assert sorted(posted) == sorted([FCM, gone.endpoint])
    assert [d.endpoint for d in db.scalars(select(PushSubscription))] == [FCM]
    assert web_push.payload_for(db.scalars(select(Notification)).first())["url"] == "/movie/329865"


def test_nothing_is_pushed_with_notifications_off_or_without_keys(keys, monkeypatch):
    db = _db()
    db.add(_device())
    db.add(UserPersonalizationSetting(user_id=1, reminders_enabled=False))
    db.add(Notification(user_id=1, kind="reminder", title="Watch Arrival", due_at=NOW))
    db.commit()
    with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(201))) as client:
        assert web_push.push_due_notifications_once(db, now=NOW, client=client) == 0
    monkeypatch.setattr(settings, "vapid_private_key", None)
    assert web_push.enabled() is False
    assert web_push.push_due_notifications_once(db, now=NOW) == 0
    assert web_push.push_due_notifications_once(_db(), now=NOW) == 0


@pytest.fixture
def signed_in():
    from app.main import app
    from app.security import get_current_user

    with SessionLocal() as db:
        user = User(username=f"push-{uuid.uuid4().hex[:8]}", password_hash="x", status="active")
        db.add(user)
        db.commit()
        user_id = user.id

    async def current():
        with SessionLocal() as db:
            return db.get(User, user_id)

    app.dependency_overrides[get_current_user] = current
    yield app, user_id
    app.dependency_overrides.pop(get_current_user, None)
    with SessionLocal() as db:
        for row in db.scalars(select(PushSubscription).where(PushSubscription.user_id == user_id)):
            db.delete(row)
        db.commit()


@pytest.mark.asyncio
async def test_a_device_subscribes_and_unsubscribes(signed_in, keys):
    app, user_id = signed_in
    _private, p256dh, auth_b64, _auth = browser()
    endpoint = f"https://fcm.googleapis.com/fcm/send/{uuid.uuid4().hex}"
    body = {"endpoint": endpoint, "keys": {"p256dh": p256dh, "auth": auth_b64}}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        assert (await client.get("/api/v1/push/key")).json() == {"public_key": keys["VAPID_PUBLIC_KEY"]}
        first = await client.post("/api/v1/push/subscriptions", json=body)
        again = await client.post("/api/v1/push/subscriptions", json=body)
        assert first.json() == {"subscribed": True, "devices": 1} and again.json()["devices"] == 1
        bad = await client.post(
            "/api/v1/push/subscriptions", json={**body, "endpoint": "https://evil.example/x"}
        )
        assert bad.status_code == 422
        removed = await client.post("/api/v1/push/subscriptions/remove", json={"endpoint": endpoint})
        assert removed.json() == {"subscribed": False}
    with SessionLocal() as db:
        assert db.scalar(select(PushSubscription).where(PushSubscription.user_id == user_id)) is None


@pytest.mark.asyncio
async def test_without_keys_the_server_says_push_is_not_set_up(signed_in, monkeypatch):
    app, _user_id = signed_in
    monkeypatch.setattr(settings, "vapid_public_key", None)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        assert (await client.get("/api/v1/push/key")).json() == {"public_key": None}
        refused = await client.post(
            "/api/v1/push/subscriptions",
            json={"endpoint": FCM, "keys": {"p256dh": "x" * 30, "auth": "y" * 10}},
        )
        assert refused.status_code == 503


def test_the_worker_pushes_only_when_keys_are_set(keys, monkeypatch):
    from sqlalchemy.exc import OperationalError

    from app.jobs import process_background

    monkeypatch.setattr(web_push, "push_due_notifications_once", lambda db: 2)
    assert process_background.push_notifications_once() == 2

    class Broken:
        def __enter__(self):
            raise OperationalError("select", {}, Exception("down"))

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(process_background, "SessionLocal", Broken)
    assert process_background.push_notifications_once() == 0
    monkeypatch.setattr(settings, "vapid_public_key", None)
    assert process_background.push_notifications_once() == 0
