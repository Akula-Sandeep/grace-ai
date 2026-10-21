import hashlib
import hmac
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import jwt
from passlib.context import CryptContext

from app.config import settings

_otp_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
_OTP_PEPPER = b"cineagent-otp-v1:"


def _peppered_otp(otp: str) -> str:
    return hmac.new(
        _OTP_PEPPER + (settings.otp_pepper or settings.jwt_secret).encode(),
        otp.encode(),
        hashlib.sha256,
    ).hexdigest()


def hash_otp(otp: str) -> str:
    if not settings.jwt_secret:
        raise RuntimeError("JWT_SECRET must be configured before issuing OTPs.")
    return _otp_context.hash(_peppered_otp(otp))


def verify_otp(otp: str, otp_hash: str) -> bool:
    if not settings.jwt_secret:
        raise RuntimeError("JWT_SECRET must be configured before verifying OTPs.")
    try:
        return _otp_context.verify(_peppered_otp(otp), otp_hash)
    except (ValueError, TypeError):
        return False


def create_access_token(
    user_id: int | str,
    expires_delta: timedelta | None = None,
    *,
    session_id: int | str | None = None,
) -> str:
    if not settings.has_configured_jwt_secret:
        raise RuntimeError("JWT_SECRET must be changed before use.")
    now = datetime.now(UTC)
    payload = {
        "sub": str(user_id),
        "iat": now,
        "exp": now + (expires_delta or timedelta(minutes=settings.access_token_ttl_minutes)),
        "jti": str(uuid4()),
        "sid": str(session_id) if session_id is not None else "",
        "iss": "Grace",
        "typ": "access",
    }
    if settings.is_production:
        payload["aud"] = "grace-api"
    return jwt.encode(
        payload,
        settings.jwt_signing_key or settings.jwt_secret,
        algorithm=settings.jwt_algorithm,
    )


def decode_access_token(token: str) -> int:
    return int(decode_access_token_claims(token)["sub"])


def decode_access_token_claims(token: str) -> dict:
    try:
        unverified = jwt.decode(token, options={"verify_signature": False, "verify_exp": False})
        exp = unverified.get("exp")
        if exp is not None:
            try:
                if float(exp) <= datetime.now(UTC).timestamp():
                    raise jwt.ExpiredSignatureError("Token has expired.")
            except (TypeError, ValueError):
                pass
    except jwt.ExpiredSignatureError:
        raise
    except (jwt.PyJWTError, TypeError, ValueError):
        pass

    payload = jwt.decode(
        token,
        settings.jwt_signing_key or settings.jwt_secret,
        algorithms=[settings.jwt_algorithm],
        audience="grace-api" if settings.is_production else None,
        issuer="Grace",
        options={
            "require": [
                "exp",
                "iat",
                "sub",
                "jti",
                "sid",
                "iss",
                "typ",
                *([] if not settings.is_production else ["aud"]),
            ]
        },
    )
    if payload.get("typ") != "access" or not payload.get("sid"):
        raise jwt.InvalidTokenError("Invalid access token type or session.")
    return payload
