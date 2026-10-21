import asyncio
import base64
import binascii
import hashlib
import hmac
import logging
import os
import re
import secrets
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit

import httpx
import jwt
from fastapi import HTTPException, Request, Response
from passlib.context import CryptContext

try:
    from argon2 import PasswordHasher
    from argon2 import exceptions as argon2_exceptions
except ImportError:  # pragma: no cover - optional dependency in dev/test envs
    PasswordHasher = None
    argon2_exceptions = None

from app.auth import create_access_token as legacy_create_access_token
from app.auth import decode_access_token, decode_access_token_claims
from app.config import settings
from app.database import SessionLocal, User
from app.observability import log_event

# scrypt cost parameters for newly created hashes. N=2**14 (the original
# setting here) is OWASP's listed *minimum* acceptable value; N=2**16 sits
# comfortably inside their recommended range while still hashing in
# ~200ms and ~64MB of memory, a reasonable cost for an interactive
# login/register endpoint that's already rate-limited
# (settings.login_rate_limit / settings.register_rate_limit).
_SCRYPT_N = 2**16
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_MAXMEM = 128 * _SCRYPT_N * _SCRYPT_R * 2  # generous headroom over the ~64MB actually used

# Hashes are self-describing (`scrypt$n=N,r=R,p=P$salt$digest`) so that
# raising these parameters in the future - or having raised them just now -
# doesn't invalidate every password already stored under the old cost. Any
# hash without that prefix is assumed to be in the legacy two-part
# `salt$digest` format and is verified with the original N=2**14 params,
# so existing users are never silently locked out by a parameter change.
_LEGACY_N, _LEGACY_R, _LEGACY_P = 2**14, 8, 1
_otp_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
_PASSWORD_HASHER = PasswordHasher() if PasswordHasher is not None else None
_PASSWORD_WORK_SEMAPHORE = asyncio.Semaphore(max(1, settings.auth_hash_concurrency))


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


def _peppered_otp(otp: str) -> str:
    return hmac.new(
        b"cineagent-otp-v1:" + settings.jwt_secret.encode(),
        otp.encode(),
        hashlib.sha256,
    ).hexdigest()


def create_access_token(data: dict, expires_delta: timedelta) -> str:
    if not settings.has_configured_jwt_secret:
        raise RuntimeError("JWT_SECRET must be changed before use.")
    payload = dict(data)
    payload["exp"] = datetime.now(UTC) + expires_delta
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def create_account_restore_token(user_id: int, deleted_at: datetime) -> str:
    if not settings.has_configured_jwt_secret:
        raise RuntimeError("JWT_SECRET must be changed before use.")
    issued_at = int(_utc_restore_datetime(deleted_at).timestamp())
    expires_at = issued_at + int(timedelta(days=settings.account_deletion_grace_days).total_seconds())
    payload = {
        "sub": str(user_id),
        "purpose": "account_restore",
        "ver": 1,
        "iat": issued_at,
        "exp": expires_at,
        "jti": secrets.token_urlsafe(16),
        "iss": "Grace",
        "typ": "account_restore",
    }
    return jwt.encode(
        payload,
        settings.jwt_signing_key or settings.jwt_secret,
        algorithm=settings.jwt_algorithm,
    )


def decode_account_restore_token(token: str) -> tuple[int, datetime] | None:
    try:
        payload = jwt.decode(
            token,
            settings.jwt_signing_key or settings.jwt_secret,
            algorithms=[settings.jwt_algorithm],
            issuer="Grace",
            options={"require": ["sub", "purpose", "ver", "iat", "exp", "jti"]},
        )
        if (
            payload.get("purpose") != "account_restore"
            or payload.get("ver") != 1
            or payload.get("typ") != "account_restore"
        ):
            return None
        user_id = int(payload["sub"])
        issued_at = datetime.fromtimestamp(int(payload["iat"]), UTC)
        if user_id <= 0:
            return None
        return user_id, issued_at
    except (jwt.PyJWTError, ValueError, TypeError, OverflowError):
        return None


def _utc_restore_datetime(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _scrypt(password, salt, n, r, p):
    maxmem = 128 * n * r * 2
    return hashlib.scrypt(password.encode(), salt=salt, n=n, r=r, p=p, maxmem=maxmem)


def hash_password(password):
    salt = os.urandom(16)
    digest = _scrypt(password, salt, _SCRYPT_N, _SCRYPT_R, _SCRYPT_P)
    params = f"n={_SCRYPT_N},r={_SCRYPT_R},p={_SCRYPT_P}"
    return (
        "scrypt$"
        + params
        + "$"
        + base64.urlsafe_b64encode(salt).decode()
        + "$"
        + base64.urlsafe_b64encode(digest).decode()
    )


def hash_password_argon2id(password: str) -> str:
    if _PASSWORD_HASHER is None:
        return hash_password(password)
    return _PASSWORD_HASHER.hash(password)


def password_strength(password: str) -> int:
    if not isinstance(password, str) or not password:
        return 0
    try:
        from zxcvbn import zxcvbn

        return int(zxcvbn(password)["score"])
    except ImportError:
        log_event(
            "password_strength_fallback",
            level=logging.WARNING,
            details={"reason": "optional_dependency_missing"},
        )
        if len(password) < 12:
            return 0
        if (
            len(password) >= 16
            and any(ch.isupper() for ch in password)
            and any(ch.isdigit() for ch in password)
        ):
            return 4
        if len(password) >= 12 and len(set(password)) >= 6:
            return 3
        return 2


class PasswordBreachCheckUnavailable(RuntimeError):
    """HIBP breach verification could not be completed."""


def is_password_allowed(password: str) -> tuple[bool, str | None]:
    if not isinstance(password, str):
        return False, "Password is required."
    if len(password) < 12 or len(password) > 128:
        return False, "Password must be 12-128 characters long."
    if password.strip() != password:
        return False, "Password cannot start or end with whitespace."
    score = password_strength(password)
    if score < 2:
        return False, "Choose a stronger password."
    return True, None


async def is_password_breached(password: str) -> bool:
    if not password:
        return False
    candidate = password.encode("utf-8")
    # SHA-1 is required by the HIBP k-anonymity range protocol, not for storage.
    digest = (
        hashlib.sha1(  # nosemgrep: python.lang.security.insecure-hash-algorithms.insecure-hash-algorithm-sha1
            candidate, usedforsecurity=False
        )
        .hexdigest()
        .upper()
    )
    prefix, suffix = digest[:5], digest[5:]
    if not prefix:
        return False
    try:
        timeout = httpx.Timeout(1.0)
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.get(
                f"https://api.pwnedpasswords.com/range/{prefix}",
                headers={
                    "User-Agent": "Grace-Auth/1.0",
                    "Add-Padding": "true",
                },
            )
            response.raise_for_status()
            return any(line.split(":", 1)[0] == suffix for line in response.text.splitlines())
    except httpx.HTTPError as exc:
        log_event(
            "password_breach_check_unavailable",
            level=logging.WARNING,
            details={
                "reason": "provider_unavailable",
                "fail_closed": settings.is_production,
            },
        )
        if settings.is_production:
            raise PasswordBreachCheckUnavailable from exc
        return False


async def async_hash_password_argon2id(password: str) -> str:
    async with _PASSWORD_WORK_SEMAPHORE:
        return await asyncio.to_thread(hash_password_argon2id, password)


async def async_verify_password(password: str, encoded: str) -> bool:
    async with _PASSWORD_WORK_SEMAPHORE:
        return await asyncio.to_thread(verify_password, password, encoded)


async def async_password_needs_rehash(encoded: str) -> bool:
    async with _PASSWORD_WORK_SEMAPHORE:
        return await asyncio.to_thread(password_needs_rehash, encoded)


def verify_password(password, encoded):
    try:
        if not encoded:
            return False
        if encoded.startswith("$argon2") and _PASSWORD_HASHER is not None:
            try:
                return _PASSWORD_HASHER.verify(encoded, password)
            except (
                argon2_exceptions.VerificationError,
                argon2_exceptions.InvalidHashError,
            ):
                return False
        parts = encoded.split("$")
        if len(parts) == 4 and parts[0] == "scrypt":
            params = dict(item.split("=") for item in parts[1].split(","))
            n, r, p = int(params["n"]), int(params["r"]), int(params["p"])
            salt_b64, digest_b64 = parts[2], parts[3]
        elif len(parts) == 2:
            n, r, p = _LEGACY_N, _LEGACY_R, _LEGACY_P
            salt_b64, digest_b64 = parts[0], parts[1]
        else:
            return False
        actual = _scrypt(password, base64.urlsafe_b64decode(salt_b64), n, r, p)
        return hmac.compare_digest(actual, base64.urlsafe_b64decode(digest_b64))
    except (ValueError, TypeError, KeyError, binascii.Error, OverflowError):
        return False


def password_needs_rehash(encoded: str) -> bool:
    if not encoded.startswith("$argon2") or _PASSWORD_HASHER is None:
        return True
    try:
        return _PASSWORD_HASHER.check_needs_rehash(encoded)
    except (argon2_exceptions.InvalidHashError, TypeError, ValueError):
        return True


def create_token(uid):
    return legacy_create_access_token(uid, session_id=0)


def decode_token(token):
    return decode_access_token(token)


def _cookie_name(prefix: str) -> str:
    # Browsers reject a ``__Host-`` cookie that is not ``Secure``, so the
    # prefix is only used when the cookie is actually issued as Secure
    # (production over HTTPS). Plain-HTTP development uses the bare name.
    return f"__Host-{prefix}" if settings.cookie_secure_enabled else prefix


def _cookie_names(prefix: str) -> tuple[str, ...]:
    return (f"__Host-{prefix}", prefix)


def read_cookie(request: Request, prefix: str) -> str | None:
    """Read a session cookie under either its secure or development name."""
    return _cookie_value(request, *_cookie_names(prefix))


def _cookie_value(request: Request, *names: str) -> str | None:
    for name in names:
        if not name:
            continue
        value = request.cookies.get(name)
        if value:
            return value
    return None


def set_access_token_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        key=_cookie_name("access_token"),
        value=token,
        httponly=True,
        secure=settings.cookie_secure_enabled,
        samesite="lax",
        max_age=settings.access_token_ttl_minutes * 60,
        path="/",
    )


def clear_access_token_cookie(response: Response) -> None:
    # Clear both spellings so a cookie issued under the other mode is removed too.
    response.delete_cookie(
        key="__Host-access_token",
        httponly=True,
        secure=True,
        samesite="lax",
        path="/",
    )
    response.delete_cookie(
        key="access_token",
        httponly=True,
        secure=settings.cookie_secure_enabled,
        samesite="lax",
        path="/",
    )


def set_refresh_token_cookie(response: Response, token: str, *, max_age_seconds: int) -> None:
    response.set_cookie(
        key=_cookie_name("refresh_token"),
        value=token,
        httponly=True,
        secure=settings.cookie_secure_enabled,
        samesite="lax",
        max_age=max_age_seconds,
        path="/",
    )


def clear_refresh_token_cookie(response: Response) -> None:
    # Clear both spellings so a cookie issued under the other mode is removed too.
    response.delete_cookie(
        key="__Host-refresh_token",
        httponly=True,
        secure=True,
        samesite="lax",
        path="/",
    )
    response.delete_cookie(
        key="refresh_token",
        httponly=True,
        secure=settings.cookie_secure_enabled,
        samesite="lax",
        path="/",
    )


def set_csrf_cookie(response: Response, token: str) -> str:
    key = (settings.csrf_key or settings.jwt_secret).encode()
    nonce, _, _ = token.partition(".")
    if not nonce:
        nonce = token
    signature = hmac.new(key, nonce.encode(), hashlib.sha256).hexdigest()
    token = f"{nonce}.{signature}"
    cookie_name = _cookie_name("csrf_token")
    response.set_cookie(
        key=cookie_name,
        value=token,
        httponly=False,
        secure=settings.cookie_secure_enabled,
        samesite="lax",
        path="/",
    )
    return token


def validate_csrf(request: Request) -> bool:
    origin = request.headers.get("origin")
    referer = request.headers.get("referer")
    source_origin = origin
    if not source_origin and referer:
        parsed_referer = urlsplit(referer)
        if parsed_referer.scheme not in {"http", "https"} or not parsed_referer.netloc:
            return False
        source_origin = f"{parsed_referer.scheme}://{parsed_referer.netloc}"
    if source_origin:
        allowed = set(settings.allowed_cors_origins)
        source_origin = source_origin.rstrip("/")
        matches_development_origin = (
            not settings.is_production
            and bool(settings.cors_origin_regex)
            and re.fullmatch(settings.cors_origin_regex, source_origin) is not None
        )
        if source_origin not in allowed and not matches_development_origin:
            return False
    cookie_token = _cookie_value(request, *_cookie_names("csrf_token"))
    header_token = request.headers.get("x-csrf-token")
    if not cookie_token or not header_token or not hmac.compare_digest(cookie_token, header_token):
        return False
    nonce, separator, signature = cookie_token.partition(".")
    if not separator or not nonce or not signature:
        return False
    expected = hmac.new(
        (settings.csrf_key or settings.jwt_secret).encode(),
        nonce.encode(),
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(signature, expected)


def _session_factory():
    try:
        from app import main as main_module
    except ImportError:
        main_module = None
    if main_module is not None and getattr(main_module, "SessionLocal", None) is not None:
        return main_module.SessionLocal
    try:
        from app.routers import auth as auth_router
    except ImportError:
        auth_router = None
    if auth_router is not None and getattr(auth_router, "SessionLocal", None) is not None:
        return auth_router.SessionLocal
    return SessionLocal


async def _resolve_user_from_request(
    request: Request,
    response: Response,
    *,
    optional: bool,
) -> User | None:
    cookie_token = _cookie_value(request, *_cookie_names("access_token"))
    token = cookie_token
    auth_header = request.headers.get("authorization")
    if auth_header and auth_header.lower().startswith("bearer "):
        token = auth_header.split(" ", 1)[1].strip() or token
    if not token:
        if optional:
            return None
        raise HTTPException(401, "Authentication required") from None

    try:
        claims = decode_access_token_claims(token)
        user_id = int(claims["sub"])
        session_id = int(claims["sid"])
    except jwt.ExpiredSignatureError:
        if optional:
            if cookie_token:
                clear_access_token_cookie(response)
            return None
        raise HTTPException(
            401,
            "Access token expired.",
            headers={"X-Problem-Code": "token_expired"},
        ) from None
    except jwt.MissingRequiredClaimError as exc:
        try:
            unverified = jwt.decode(token, options={"verify_signature": False, "verify_exp": False})
            expired = float(unverified.get("exp", 0)) <= datetime.now(UTC).timestamp()
        except (jwt.PyJWTError, TypeError, ValueError):
            expired = False
        if exc.claim == "exp" or expired:
            if optional:
                if cookie_token:
                    clear_access_token_cookie(response)
                return None
            raise HTTPException(
                401,
                "Access token expired.",
                headers={"X-Problem-Code": "token_expired"},
            ) from None
        if optional:
            if cookie_token:
                clear_access_token_cookie(response)
            return None
        raise HTTPException(
            401,
            "Access token is invalid.",
            headers={"X-Problem-Code": "invalid_token"},
        ) from None
    except (jwt.PyJWTError, TypeError, ValueError):
        if optional:
            if cookie_token:
                clear_access_token_cookie(response)
            return None
        raise HTTPException(
            401,
            "Access token is invalid.",
            headers={"X-Problem-Code": "invalid_token"},
        ) from None

    def load_user() -> User | None:
        session_factory = _session_factory()
        with session_factory() as db:
            return db.get(User, user_id)

    user = await asyncio.to_thread(load_user)
    if user is None:
        if optional:
            if cookie_token:
                clear_access_token_cookie(response)
            return None
        raise HTTPException(
            401,
            "Account is unavailable.",
            headers={"X-Problem-Code": "invalid_token"},
        ) from None

    def session_active() -> bool:
        with _session_factory()() as db:
            session = db.get(RefreshSession, session_id)
            if session is None or session.user_id != user_id or session.revoked_at is not None:
                return False
            now = datetime.now(UTC)
            idle_expires_at = session.idle_expires_at
            absolute_expires_at = session.absolute_expires_at
            if idle_expires_at and idle_expires_at.tzinfo is None:
                idle_expires_at = idle_expires_at.replace(tzinfo=UTC)
            if absolute_expires_at and absolute_expires_at.tzinfo is None:
                absolute_expires_at = absolute_expires_at.replace(tzinfo=UTC)
            return not (
                (idle_expires_at and idle_expires_at <= now)
                or (absolute_expires_at and absolute_expires_at <= now)
            )

    from app.database import RefreshSession

    if not await asyncio.to_thread(session_active):
        if optional and cookie_token:
            clear_access_token_cookie(response)
            return None
        raise HTTPException(
            401,
            "Session is no longer active.",
            headers={"X-Problem-Code": "session_revoked"},
        ) from None
    if (
        not user.is_active
        or user.status in {"disabled", "deleted", "suspended"}
        or (user.locked_until and user.locked_until > datetime.now(UTC))
    ):
        raise HTTPException(403, "Account is unavailable.") from None
    return user


async def get_current_user_optional(
    request: Request,
    response: Response = None,
) -> User | None:
    if response is None:
        response = Response()
    return await _resolve_user_from_request(request, response, optional=True)


async def get_current_user(request: Request, response: Response) -> User:
    user = await _resolve_user_from_request(request, response, optional=False)
    if user is None:
        raise HTTPException(401, "Authentication required") from None
    return user
