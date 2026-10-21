from datetime import UTC, datetime, timedelta

import jwt

from app.config import settings
from app.security import create_account_restore_token, decode_account_restore_token


def test_account_restore_token_roundtrip():
    deleted_at = datetime.now(UTC) - timedelta(minutes=2)
    token = create_account_restore_token(42, deleted_at)

    result = decode_account_restore_token(token)
    assert result is not None
    user_id, token_deleted_at = result
    assert user_id == 42
    assert int(token_deleted_at.timestamp()) == int(deleted_at.timestamp())


def test_account_restore_token_is_purpose_bound_and_contains_no_email():
    deleted_at = datetime.now(UTC) - timedelta(minutes=1)
    token = create_account_restore_token(99, deleted_at)
    payload = jwt.decode(
        token,
        settings.jwt_signing_key or settings.jwt_secret,
        algorithms=[settings.jwt_algorithm],
        options={"verify_exp": False},
    )

    assert payload["purpose"] == "account_restore"
    assert payload["typ"] == "account_restore"
    assert "email" not in payload
    assert "password" not in payload


def test_account_restore_token_rejects_tampering():
    token = create_account_restore_token(7, datetime.now(UTC) - timedelta(seconds=5))
    header, payload, signature = token.split(".")
    tampered = f"{header}.{payload[:-1] + ('0' if payload[-1] != '0' else '1')}.{signature}"
    assert decode_account_restore_token(tampered) is None


def test_account_restore_token_rejects_expired_token():
    deleted_at = datetime.now(UTC) - timedelta(days=settings.account_deletion_grace_days, seconds=1)
    token = create_account_restore_token(8, deleted_at)
    assert decode_account_restore_token(token) is None
