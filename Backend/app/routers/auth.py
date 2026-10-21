import asyncio
import hashlib
import json
import re
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import case, delete, func, select, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from app.auth import create_access_token, hash_otp, verify_otp
from app.config import settings
from app.database import (
    AuditLog,
    Conversation,
    ExportJob,
    Feedback,
    GuestAdoption,
    Memory,
    Message,
    Movie,
    OTPDeliveryJob,
    OTPVerification,
    PendingEmailChange,
    Preference,
    RefreshSession,
    SessionLocal,
    User,
    UserInterfaceSetting,
    UserPersonalizationSetting,
    WatchedItem,
    WatchlistItem,
)
from app.database import run_db as _run_db_with_factory
from app.guest_session import read_guest_session_id
from app.observability import logger
from app.rate_limit import (
    OTP_SEND_EMAIL_LIMIT,
    OTP_SEND_IP_LIMIT,
    OTP_SEND_WINDOW_SECONDS,
    OTP_VERIFY_EMAIL_LIMIT,
    OTP_VERIFY_IP_LIMIT,
    OTP_VERIFY_WINDOW_SECONDS,
)
from app.security import (
    PasswordBreachCheckUnavailable,
    async_hash_password_argon2id,
    async_password_needs_rehash,
    async_verify_password,
    clear_access_token_cookie,
    clear_refresh_token_cookie,
    create_account_restore_token,
    decode_account_restore_token,
    get_current_user,
    hash_password,
    hash_password_argon2id,
    is_password_allowed,
    is_password_breached,
    read_cookie,
    set_access_token_cookie,
    set_csrf_cookie,
    set_refresh_token_cookie,
    validate_csrf,
)
from app.services.export_service import hash_download_token
from app.services.otp_delivery_service import encrypt_code
from app.shared_state import shared_state


def run_db(fn):
    """Run a synchronous ORM unit off the event loop using this module's session factory."""
    return _run_db_with_factory(fn, session_factory=SessionLocal)


router = APIRouter(prefix="/auth", tags=["authentication"])
ACCESS_TOKEN_TTL = timedelta(minutes=settings.access_token_ttl_minutes)
OTP_ATTEMPT_LIMIT = 5
AUTH_FAIL_LIMIT = 10
AUTH_CAPTCHA_FAILURES = 5
AUTH_LOCKOUT_SECONDS = 900
_DUMMY_AUTH_HASH = hash_password_argon2id("phase2-fixed-dummy-password-never-used")


def _utc_aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _set_access_token_expiry_header(response: Response) -> None:
    expires_at = datetime.now(UTC) + ACCESS_TOKEN_TTL
    response.headers["X-Access-Token-Expires-At"] = str(int(expires_at.timestamp()))


def otp_ttl() -> timedelta:
    return timedelta(minutes=settings.otp_expire_minutes)


async def _equalize_auth_timing(password: str, user: User | None) -> bool:
    return await async_verify_password(
        password,
        user.password_hash if user is not None else _DUMMY_AUTH_HASH,
    )


async def _check_auth_attempt_budget(request: Request, email: str) -> None:
    client_ip = request.client.host if request.client else "unknown"
    normalized = _normalize_email(email)
    retry_after = "60"
    if not await shared_state.rate_limit(f"auth-ip:{client_ip}", 60, 60):
        raise HTTPException(
            429,
            "Too many requests from this IP. Please try again shortly.",
            headers={"Retry-After": retry_after},
        ) from None
    if not await shared_state.rate_limit(f"auth-ip-account:{client_ip}:{normalized}", 25, 60):
        raise HTTPException(
            429,
            "Too many requests for this account from this IP. Please try again shortly.",
            headers={"Retry-After": retry_after},
        ) from None


def _minutes(seconds: int) -> int:
    return max(1, round(seconds / 60))


async def _check_otp_ip_budget(request: Request) -> None:
    """Per-IP budget for *sending* codes (sign-up, resend, password reset)."""
    client_ip = request.client.host if request.client else "unknown"
    if not await shared_state.rate_limit(
        f"otp-send-ip:{client_ip}", OTP_SEND_IP_LIMIT, OTP_SEND_WINDOW_SECONDS
    ):
        raise HTTPException(
            429,
            f"Too many codes were requested from this network. Try again in about {_minutes(OTP_SEND_WINDOW_SECONDS)} minutes.",
            headers={"Retry-After": str(OTP_SEND_WINDOW_SECONDS), "X-Problem-Code": "otp_send_limited"},
        ) from None


async def _check_otp_send_email_budget(email: str) -> None:
    """Per-address budget for sending codes, so one inbox can't be flooded."""
    if not await shared_state.rate_limit(
        f"otp-send-email:{_normalize_email(email)}",
        OTP_SEND_EMAIL_LIMIT,
        OTP_SEND_WINDOW_SECONDS,
    ):
        raise HTTPException(
            429,
            "Too many codes were sent to this email recently. Use the latest code, or try again later.",
            headers={"Retry-After": str(OTP_SEND_WINDOW_SECONDS), "X-Problem-Code": "otp_send_limited"},
        ) from None


async def _check_otp_verify_budget(request: Request, email: str) -> None:
    """Budget for *checking* codes, separate from sending them."""
    client_ip = request.client.host if request.client else "unknown"
    allowed = await shared_state.rate_limit(
        f"otp-verify-ip:{client_ip}", OTP_VERIFY_IP_LIMIT, OTP_VERIFY_WINDOW_SECONDS
    ) and await shared_state.rate_limit(
        f"otp-verify-email:{_normalize_email(email)}",
        OTP_VERIFY_EMAIL_LIMIT,
        OTP_VERIFY_WINDOW_SECONDS,
    )
    if not allowed:
        raise HTTPException(
            429,
            f"Too many attempts. Wait about {_minutes(OTP_VERIFY_WINDOW_SECONDS)} minutes, then request a new code.",
            headers={"Retry-After": str(OTP_VERIFY_WINDOW_SECONDS), "X-Problem-Code": "otp_verify_limited"},
        ) from None


_OTP_FAILURES = {
    "expired": ("otp_expired", "That code has expired. Request a new one and use the latest email."),
    "locked": ("otp_locked", "Too many wrong attempts for that code. Request a new one."),
    "invalid": ("otp_invalid", "That code isn’t right. Check the latest email from Grace and try again."),
}


def _otp_failure(result: str) -> HTTPException:
    code, message = _OTP_FAILURES.get(result, _OTP_FAILURES["invalid"])
    return HTTPException(400, message, headers={"X-Problem-Code": code})


async def _record_failed_login(email: str) -> bool:
    normalized = _normalize_email(email)
    now = datetime.now(UTC)

    def op(db):
        locked_until = now + timedelta(seconds=AUTH_LOCKOUT_SECONDS)
        stmt = (
            update(User)
            .where(User.email == normalized)
            .values(
                failed_login_attempts=User.failed_login_attempts + 1,
                locked_until=case(
                    (
                        (User.failed_login_attempts + 1) >= AUTH_FAIL_LIMIT,
                        locked_until,
                    ),
                    else_=User.locked_until,
                ),
            )
            .returning(User.failed_login_attempts)
        )
        new_count = db.execute(stmt).scalar_one_or_none()
        if new_count is None:
            return False
        db.commit()
        return int(new_count) >= AUTH_FAIL_LIMIT

    return await run_db(op)


def _failing_ip_key(email: str) -> str:
    return "auth-fail-ips:" + hashlib.sha256(_normalize_email(email).encode()).hexdigest()


async def _remember_failing_ip(email: str, client_ip: str) -> None:
    """Track which client IPs produced failures, so a lockout cannot be weaponised
    against the account owner signing in from a different network."""
    key = _failing_ip_key(email)
    known = await shared_state.cache_get(key, AUTH_LOCKOUT_SECONDS)
    known = known if isinstance(known, list) else []
    if client_ip not in known:
        known = [*known[-49:], client_ip]
    await shared_state.cache_set(key, known, settings.cache_max_entries, ttl=AUTH_LOCKOUT_SECONDS)


async def _ip_contributed_to_lockout(email: str, client_ip: str) -> bool:
    known = await shared_state.cache_get(_failing_ip_key(email), AUTH_LOCKOUT_SECONDS)
    # Without attribution data (e.g. cache lost) fail closed: the lock applies.
    return not isinstance(known, list) or not known or client_ip in known


async def _clear_failed_login(email: str) -> None:
    normalized = _normalize_email(email)

    def op(db):
        db.execute(
            update(User).where(User.email == normalized).values(failed_login_attempts=0, locked_until=None)
        )
        db.commit()

    await run_db(op)


async def _is_account_locked(email: str) -> bool:
    normalized = _normalize_email(email)
    now = datetime.now(UTC)

    def op(db):
        user = db.scalar(select(User).where(User.email == normalized))
        if user is None:
            return False
        locked_until = _utc_aware(user.locked_until)
        if locked_until and locked_until > now:
            return True
        if locked_until and locked_until <= now:
            db.execute(
                update(User).where(User.id == user.id).values(failed_login_attempts=0, locked_until=None)
            )
            db.commit()
        return False

    return await run_db(op)


class EmailRequest(BaseModel):
    email: str = Field(min_length=3, max_length=254)

    @field_validator("email")
    @classmethod
    def validate_email(cls, value: str) -> str:
        normalized = value.strip().lower()
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", normalized):
            raise ValueError("Enter a valid email address.")
        return normalized


class OTPVerificationRequest(EmailRequest):
    code: str = Field(
        pattern=r"^\d{6}$",
        validation_alias=AliasChoices("code", "otp"),
    )


class RegisterRequest(EmailRequest):
    password: str = Field(min_length=12, max_length=128)


class LoginRequest(EmailRequest):
    password: str = Field(min_length=12, max_length=128)


class ResetRequest(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    code: str = Field(pattern=r"^\d{6}$")
    password: str = Field(min_length=12, max_length=128)

    @field_validator("email")
    @classmethod
    def validate_email(cls, value: str) -> str:
        return value.strip().lower()


class ProfileUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: str | None = Field(default=None, min_length=2, max_length=30)
    display_name: str | None = Field(default=None, max_length=60)

    @field_validator("display_name")
    @classmethod
    def validate_display_name(cls, value: str | None) -> str | None:
        if value is None:
            return value
        normalized = " ".join(value.split())
        if any(ord(character) < 32 or ord(character) == 127 for character in normalized):
            raise ValueError("Use a name without control characters.")
        if not 1 <= len(normalized) <= 60:
            raise ValueError("Use a name between 1 and 60 characters.")
        return normalized

    @field_validator("username")
    @classmethod
    def validate_username(cls, value: str | None) -> str | None:
        if value is None:
            return value
        normalized = value.strip()
        if not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9_.-]{0,28}[A-Za-z0-9])", normalized):
            raise ValueError("Use 2-30 letters, numbers, dots, underscores, or hyphens.")
        return normalized


class PasswordChangeRequest(BaseModel):
    current_password: str = Field(min_length=12, max_length=128)
    new_password: str = Field(min_length=12, max_length=128)


class PersonalizationUpdateRequest(BaseModel):
    memory_enabled: bool | None = None
    reminders_enabled: bool | None = None
    answer_style: str | None = Field(default=None, pattern=r"^(brief|balanced|detailed)$")
    daily_brief_enabled: bool | None = None


def _personalization(setting: UserPersonalizationSetting | None) -> dict:
    return {
        "memory_enabled": (
            setting.memory_enabled if setting and setting.memory_enabled is not None else True
        ),
        "reminders_enabled": (
            setting.reminders_enabled if setting and setting.reminders_enabled is not None else True
        ),
        "answer_style": (setting.answer_style if setting else None) or "balanced",
        "daily_brief_enabled": bool(setting.daily_brief_enabled) if setting else False,
    }


class UserInterfaceSettingsUpdateRequest(BaseModel):
    theme: str | None = Field(default=None, pattern=r"^(light|dark|system)$")
    density: str | None = Field(default=None, pattern=r"^(comfortable|compact)$")
    region: str | None = Field(default=None, pattern=r"^[A-Za-z]{2}$")
    motion: str | None = Field(default=None, pattern=r"^(system|reduced)$")


class PreferenceUpdateRequest(BaseModel):
    dimension: str | None = Field(default=None, min_length=1, max_length=120)
    value: str | None = Field(default=None, min_length=1, max_length=300)
    polarity: str | None = Field(default=None, pattern=r"^(prefer|avoid)$")
    strength: float | None = Field(default=None, ge=0, le=1)


class EmailChangeVerifyRequest(BaseModel):
    code: str = Field(pattern=r"^\d{6}$")


class AccountDeletionRequest(BaseModel):
    current_password: str | None = Field(default=None, min_length=1, max_length=128)


class UserFeedbackRequest(BaseModel):
    text: str = Field(min_length=5, max_length=5000)
    idempotency_key: str | None = Field(default=None, min_length=8, max_length=128)
    category: Literal[
        "general",
        "wrong_answer",
        "bad_recommendation",
        "incorrect_source",
        "broken_ui",
        "feature",
    ] = "general"


class OnboardingRequest(BaseModel):
    favorite_genres: list[str] = Field(default_factory=list, max_length=12)
    avoid_genres: list[str] = Field(default_factory=list, max_length=12)
    avoid_traits: list[str] = Field(default_factory=list, max_length=12)
    favorite_movies: list[str] = Field(default_factory=list, max_length=5)


def _normalize_email(value: str) -> str:
    return value.strip().lower()


async def _assert_password_not_breached(password: str) -> None:
    try:
        breached = await is_password_breached(password)
    except PasswordBreachCheckUnavailable:
        raise HTTPException(
            503,
            "Password security verification is temporarily unavailable. Please try again shortly.",
        ) from None
    if breached:
        raise HTTPException(
            400,
            "This password has appeared in a known breach; choose another password.",
        ) from None


def _hash_refresh_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _same_shape_response(message: str, *, status: str = "success", **extra) -> dict:
    return {"status": status, "message": message, **extra}


def _code_sent_response(message: str) -> dict:
    """Same shape whether or not the account exists; tells the client the real expiry."""
    return _same_shape_response(message, status="accepted", expires_in_minutes=settings.otp_expire_minutes)


def _user_profile(user: User) -> dict:
    return {
        "id": user.id,
        "email": user.email,
        "username": user.username,
        "display_name": user.display_name,
        "status": user.status,
        "email_verified": bool(user.email_verified),
    }


async def _get_user_by_email(email: str) -> User | None:
    normalized = _normalize_email(email)
    return await run_db(lambda db: db.scalar(select(User).where(User.email == normalized)))


def _issue_session(user: User, request: Request, response: Response) -> None:
    csrf_token = secrets.token_urlsafe(16)
    refresh_token = secrets.token_urlsafe(32)
    now = datetime.now(UTC)
    with SessionLocal() as db:
        session = RefreshSession(
            user_id=user.id,
            refresh_hash=_hash_refresh_token(refresh_token),
            user_agent=request.headers.get("user-agent"),
            ip_address=_privacy_ip(request.client.host if request.client else None),
            created_at=now,
            last_used=now,
            idle_expires_at=now + timedelta(hours=settings.session_idle_timeout_hours),
            absolute_expires_at=now + timedelta(days=settings.session_absolute_timeout_days),
        )
        db.add(session)
        db.commit()
        db.refresh(session)
        access_token = create_access_token(user.id, ACCESS_TOKEN_TTL, session_id=session.id)
        db.execute(
            delete(RefreshSession).where(
                RefreshSession.user_id == user.id,
                RefreshSession.id.not_in(
                    select(RefreshSession.id)
                    .where(RefreshSession.user_id == user.id)
                    .order_by(RefreshSession.created_at.desc())
                    .limit(settings.max_sessions_per_user)
                ),
            )
        )
        db.commit()
    set_access_token_cookie(response, access_token)
    set_refresh_token_cookie(response, refresh_token, max_age_seconds=settings.cookie_max_age_seconds)
    response.headers["X-CSRF-Token"] = set_csrf_cookie(response, csrf_token)
    _set_access_token_expiry_header(response)


def _update_password_hash(db, user_id: int, password_hash_value: str) -> None:
    user = db.get(User, user_id)
    if user is None:
        return
    user.password_hash = password_hash_value
    db.commit()


async def _issue_session_async(user_id: int, request: Request, response: Response) -> None:
    csrf_token = secrets.token_urlsafe(16)
    refresh_token = secrets.token_urlsafe(32)
    now = datetime.now(UTC)

    def op(db):
        session = RefreshSession(
            user_id=user_id,
            refresh_hash=_hash_refresh_token(refresh_token),
            user_agent=request.headers.get("user-agent"),
            ip_address=_privacy_ip(request.client.host if request.client else None),
            created_at=now,
            last_used=now,
            idle_expires_at=now + timedelta(hours=settings.session_idle_timeout_hours),
            absolute_expires_at=now + timedelta(days=settings.session_absolute_timeout_days),
        )
        db.add(session)
        db.flush()
        access_token = create_access_token(user_id, ACCESS_TOKEN_TTL, session_id=session.id)
        db.execute(
            delete(RefreshSession).where(
                RefreshSession.user_id == user_id,
                RefreshSession.id.not_in(
                    select(RefreshSession.id)
                    .where(RefreshSession.user_id == user_id)
                    .order_by(RefreshSession.created_at.desc())
                    .limit(settings.max_sessions_per_user)
                ),
            )
        )
        db.commit()
        return access_token

    access_token = await run_db(op)
    set_access_token_cookie(response, access_token)
    set_refresh_token_cookie(response, refresh_token, max_age_seconds=settings.cookie_max_age_seconds)
    response.headers["X-CSRF-Token"] = set_csrf_cookie(response, csrf_token)
    _set_access_token_expiry_header(response)


def _privacy_ip(value: str | None) -> str | None:
    if not value:
        return None
    if ":" in value:
        return value.rsplit(":", 1)[0] + ":"
    octets = value.split(".")
    return ".".join(octets[:3]) + ".0" if len(octets) == 4 else value


def _store_otp(email: str, otp_hash: str, purpose: str = "verification") -> None:
    now = datetime.now(UTC)
    expires_at = now + otp_ttl()
    with SessionLocal() as db:
        db.execute(
            update(OTPVerification)
            .where(
                OTPVerification.email == email,
                OTPVerification.purpose == purpose,
                OTPVerification.is_used.is_(False),
            )
            .values(is_used=True)
        )
        db.add(
            OTPVerification(
                email=email,
                purpose=purpose,
                otp_hash=otp_hash,
                expires_at=expires_at,
                attempts=0,
                is_used=False,
                created_at=now,
            )
        )
        db.commit()


def _has_recent_otp(email: str) -> bool:
    cutoff = datetime.now(UTC) - timedelta(seconds=60)
    with SessionLocal() as db:
        return (
            db.scalar(
                select(OTPVerification.id)
                .where(
                    OTPVerification.email == email,
                    OTPVerification.is_used.is_(False),
                    OTPVerification.created_at >= cutoff,
                )
                .limit(1)
            )
            is not None
        )


def _consume_otp(email: str, code: str, purpose: str = "verification") -> tuple[int | None, str]:
    now = datetime.now(UTC)
    with SessionLocal() as db:
        verification = db.scalar(
            select(OTPVerification)
            .where(
                OTPVerification.email == email,
                OTPVerification.purpose == purpose,
                OTPVerification.is_used.is_(False),
            )
            .order_by(OTPVerification.created_at.desc(), OTPVerification.id.desc())
            .with_for_update()
            .limit(1)
        )
        if verification is None:
            return None, "invalid"

        expires_at = _utc_aware(verification.expires_at)
        if expires_at <= now:
            db.delete(verification)
            db.commit()
            return None, "expired"
        if not verify_otp(code, verification.otp_hash):
            db.execute(
                update(OTPVerification)
                .where(
                    OTPVerification.id == verification.id,
                    OTPVerification.is_used.is_(False),
                )
                .values(attempts=OTPVerification.attempts + 1)
                .execution_options(synchronize_session=False)
            )
            db.refresh(verification)
            if verification.attempts > OTP_ATTEMPT_LIMIT:
                verification.is_used = True
            db.commit()
            return None, "locked" if verification.is_used else "invalid"

        consumed = db.execute(
            update(OTPVerification)
            .where(
                OTPVerification.id == verification.id,
                OTPVerification.is_used.is_(False),
                OTPVerification.expires_at > now,
            )
            .values(is_used=True)
            .execution_options(synchronize_session=False)
        )
        if consumed.rowcount != 1:
            db.rollback()
            return None, "invalid"
        user = db.scalar(select(User).where(User.email == email))
        if user is None:
            username = "email_" + secrets.token_hex(16)
            user = User(
                username=username,
                password_hash=hash_password(secrets.token_urlsafe(32)),
                email=email,
            )
            db.add(user)
        db.commit()
        return user.id, "verified"


async def _deliver_otp_email(email: str, code: str, purpose: str = "verification") -> None:
    now = datetime.now(UTC)
    ciphertext = encrypt_code(code)

    def enqueue(db):
        db.add(
            OTPDeliveryJob(
                email=email,
                code_ciphertext=ciphertext,
                purpose=purpose,
                status="pending",
                available_at=now,
                created_at=now,
            )
        )
        db.commit()

    await run_db(enqueue)


@router.get("/csrf")
async def csrf_bootstrap(response: Response):
    csrf_token = secrets.token_urlsafe(16)
    response.headers["X-CSRF-Token"] = set_csrf_cookie(response, csrf_token)
    return {"status": "success"}


@router.post("/logout")
def logout(request: Request, response: Response):
    if settings.is_production and not validate_csrf(request):
        raise HTTPException(403, "CSRF validation failed.") from None
    refresh_token = read_cookie(request, "refresh_token")
    if refresh_token:
        with SessionLocal() as db:
            db.execute(
                update(RefreshSession)
                .where(RefreshSession.refresh_hash == _hash_refresh_token(refresh_token))
                .values(revoked_at=datetime.now(UTC))
            )
            db.commit()
    clear_access_token_cookie(response)
    clear_refresh_token_cookie(response)
    return {"message": "Signed out successfully"}


@router.post("/register")
async def register_phase2(payload: RegisterRequest, request: Request, response: Response):
    email = _normalize_email(payload.email)
    await _check_otp_ip_budget(request)
    await _check_otp_send_email_budget(email)
    await _check_auth_attempt_budget(request, email)
    ok, issue = is_password_allowed(payload.password)
    if not ok:
        raise HTTPException(400, issue or "Password is invalid.") from None
    await _assert_password_not_breached(payload.password)
    user = await _get_user_by_email(email)
    if user is None:
        username = "user_" + secrets.token_hex(8)
        password_hash = await async_hash_password_argon2id(payload.password)

        def create_user(db):
            account = User(
                username=username,
                email=email,
                password_hash=password_hash,
                status="pending",
                email_verified=False,
            )
            db.add(account)
            db.flush()
            user_id = account.id
            db.commit()
            return user_id

        try:
            await run_db(create_user)
        except IntegrityError:
            raise HTTPException(409, "An account with that email already exists.") from None
    elif user.email_verified:
        return _code_sent_response("If the account exists, a verification code has been sent.")
    else:
        new_hash = await async_hash_password_argon2id(payload.password)

        def op(db):
            current = db.get(User, user.id)
            if current is None:
                return
            current.password_hash = new_hash
            current.failed_login_attempts = 0
            current.locked_until = None
            db.execute(
                update(RefreshSession)
                .where(
                    RefreshSession.user_id == current.id,
                    RefreshSession.revoked_at.is_(None),
                )
                .values(revoked_at=datetime.now(UTC), revoked_reason="reregister")
            )
            db.commit()

        await run_db(op)
    code = "".join(secrets.choice("0123456789") for _ in range(6))
    otp_hash = await asyncio.to_thread(hash_otp, code)
    await asyncio.to_thread(_store_otp, email, otp_hash, "verification")
    await _deliver_otp_email(email, code, "verification")
    return _code_sent_response("We sent a 6-digit code to your email.")


@router.post("/verify")
async def verify_phase2(payload: OTPVerificationRequest, request: Request, response: Response):
    await _check_otp_verify_budget(request, payload.email)
    user_id, result = await asyncio.to_thread(_consume_otp, payload.email, payload.code, "verification")
    if result != "verified" or user_id is None:
        raise _otp_failure(result) from None

    def verify_user_tx(db):
        user = db.get(User, user_id)
        if user is None:
            return None
        user.email_verified = True
        user.status = "verified"
        db.commit()
        return user_id

    verified_user_id = await run_db(verify_user_tx)
    if verified_user_id is None:
        raise HTTPException(404, "User not found.") from None
    await _issue_session_async(verified_user_id, request, response)
    await _adopt_guest_state_best_effort(request, response, verified_user_id)
    return _same_shape_response("Email verified.")


@router.post("/resend")
async def resend_phase2(payload: EmailRequest, request: Request):
    await _check_otp_ip_budget(request)
    email = _normalize_email(payload.email)
    await _check_otp_send_email_budget(email)
    if await _get_user_by_email(email) is None:
        return _code_sent_response("If the account exists, a new code will be sent.")
    if await asyncio.to_thread(_has_recent_otp, email):
        return _code_sent_response(
            "A code was sent less than a minute ago. Check your inbox and spam folder."
        )
    code = "".join(secrets.choice("0123456789") for _ in range(6))
    otp_hash = await asyncio.to_thread(hash_otp, code)
    await asyncio.to_thread(_store_otp, email, otp_hash, "verification")
    await _deliver_otp_email(email, code, "verification")
    return _code_sent_response("We sent you a new code. Only the latest code works.")


@router.post("/login")
async def login_phase2(payload: LoginRequest, request: Request, response: Response):
    email = _normalize_email(payload.email)
    await _check_auth_attempt_budget(request, email)
    client_ip = request.client.host if request.client else "unknown"
    if await _is_account_locked(email) and await _ip_contributed_to_lockout(email, client_ip):
        raise HTTPException(
            401,
            "Invalid email or password.",
            headers={"X-Problem-Code": "invalid_credentials"},
        ) from None
    user = await _get_user_by_email(email)
    valid_password = await _equalize_auth_timing(payload.password, user)
    if user is None or not valid_password:
        await _remember_failing_ip(email, client_ip)
        await _record_failed_login(email)
        raise HTTPException(
            401,
            "Invalid email or password.",
            headers={"X-Problem-Code": "invalid_credentials"},
        ) from None
    if user.status == "deleted" or user.deleted_at is not None:
        raise HTTPException(
            403,
            "This account is scheduled for deletion. Restore it before signing in.",
            headers={"X-Problem-Code": "account_deleted"},
        ) from None
    if user.failed_login_attempts > 0:
        await _clear_failed_login(email)
    if not user.email_verified:
        raise HTTPException(
            401,
            "Invalid email or password.",
            headers={"X-Problem-Code": "invalid_credentials"},
        ) from None
    if await async_password_needs_rehash(user.password_hash):
        new_hash = await async_hash_password_argon2id(payload.password)
        await run_db(lambda db: _update_password_hash(db, user.id, new_hash))
    await _issue_session_async(user.id, request, response)
    # Like signing up, logging in keeps what you did as a guest on this device.
    await _adopt_guest_state_best_effort(request, response, user.id)
    return _same_shape_response("Authenticated.")


@router.post("/refresh")
def refresh_phase2(request: Request, response: Response):
    refresh_token = read_cookie(request, "refresh_token")
    if not refresh_token:
        raise HTTPException(401, "Refresh token missing.") from None
    token_hash = _hash_refresh_token(refresh_token)
    with SessionLocal() as db:
        session = db.scalar(
            select(RefreshSession)
            .where(
                RefreshSession.refresh_hash == token_hash,
                RefreshSession.revoked_at.is_(None),
            )
            .with_for_update()
        )
        if session is None:
            rotated = db.scalar(
                select(RefreshSession).where(
                    RefreshSession.prev_token_hash == token_hash,
                    RefreshSession.revoked_at.is_(None),
                )
            )
            if rotated is not None:
                now = datetime.now(UTC)
                db.execute(
                    update(RefreshSession)
                    .where(RefreshSession.family_id == rotated.family_id)
                    .values(revoked_at=now, revoked_reason="refresh_reuse")
                )
                db.add(
                    AuditLog(
                        user_id=rotated.user_id,
                        session_id=rotated.id,
                        event_type="refresh_token_reuse",
                        metadata_json=json.dumps({"family_id": rotated.family_id}),
                    )
                )
                db.commit()
            raise HTTPException(401, "Refresh token is invalid or revoked.") from None
        now = datetime.now(UTC)
        idle_expires = _utc_aware(session.idle_expires_at)
        absolute_expires = _utc_aware(session.absolute_expires_at)
        if (idle_expires and idle_expires <= now) or (absolute_expires and absolute_expires <= now):
            session.revoked_at = now
            session.revoked_reason = "expired"
            db.commit()
            raise HTTPException(401, "Refresh session expired.") from None
        user = db.get(User, session.user_id)
        if user is None or not user.email_verified:
            raise HTTPException(401, "Account is unavailable.") from None
        session.last_used = now
        session.prev_token_hash = session.refresh_hash
        new_refresh = secrets.token_urlsafe(32)
        session.refresh_hash = _hash_refresh_token(new_refresh)
        session.idle_expires_at = now + timedelta(hours=settings.session_idle_timeout_hours)
        db.commit()
        session_id = session.id
    access_token = create_access_token(user.id, ACCESS_TOKEN_TTL, session_id=session_id)
    set_access_token_cookie(response, access_token)
    set_refresh_token_cookie(response, new_refresh, max_age_seconds=settings.cookie_max_age_seconds)
    csrf_token = secrets.token_urlsafe(16)
    response.headers["X-CSRF-Token"] = set_csrf_cookie(response, csrf_token)
    _set_access_token_expiry_header(response)
    return _same_shape_response("Session refreshed.")


@router.post("/logout-all")
def logout_all_phase2(request: Request, response: Response, user: User = Depends(get_current_user)):
    if settings.is_production and not validate_csrf(request):
        raise HTTPException(403, "CSRF validation failed.") from None
    with SessionLocal() as db:
        db.execute(
            update(RefreshSession)
            .where(RefreshSession.user_id == user.id)
            .values(revoked_at=datetime.now(UTC))
        )
        db.commit()
    clear_access_token_cookie(response)
    clear_refresh_token_cookie(response)
    response.delete_cookie("grace_restore_token", path="/api/v1/auth")
    return _same_shape_response("All sessions revoked.")


@router.post("/forgot")
async def forgot_phase2(payload: EmailRequest, request: Request):
    email = _normalize_email(payload.email)
    await _check_otp_ip_budget(request)
    await _check_otp_send_email_budget(email)
    await _check_auth_attempt_budget(request, email)
    user = await _get_user_by_email(email)
    if user is not None and user.email_verified:
        code = "".join(secrets.choice("0123456789") for _ in range(6))
        otp_hash = await asyncio.to_thread(hash_otp, code)
        await asyncio.to_thread(_store_otp, email, otp_hash, "password_reset")
        await _deliver_otp_email(email, code, "password_reset")
    return _code_sent_response("If an account exists for that email, we sent a reset code.")


@router.post("/reset")
async def reset_phase2(payload: ResetRequest, request: Request):
    email = _normalize_email(payload.email)
    await _check_otp_verify_budget(request, email)
    ok, issue = is_password_allowed(payload.password)
    if not ok:
        raise HTTPException(400, issue or "Password is invalid.") from None
    await _assert_password_not_breached(payload.password)
    user_id, result = await asyncio.to_thread(_consume_otp, email, payload.code, "password_reset")
    if result != "verified" or user_id is None:
        raise _otp_failure(result) from None
    new_hash = await async_hash_password_argon2id(payload.password)

    def reset_tx(db):
        user = db.get(User, user_id)
        if user is None:
            return False
        user.password_hash = new_hash
        user.failed_login_attempts = 0
        user.locked_until = None
        db.execute(
            update(RefreshSession)
            .where(RefreshSession.user_id == user_id, RefreshSession.revoked_at.is_(None))
            .values(revoked_at=datetime.now(UTC), revoked_reason="password_reset")
        )
        db.commit()
        return True

    if not await run_db(reset_tx):
        raise HTTPException(404, "Account not found.") from None
    return _same_shape_response("Password reset completed.")


@router.get("/me")
async def get_me(response: Response, user: User = Depends(get_current_user)):
    _set_access_token_expiry_header(response)
    return _user_profile(user)


@router.get("/me/sessions")
def list_sessions(request: Request, user: User = Depends(get_current_user)):
    refresh_token = read_cookie(request, "refresh_token")
    current_hash = _hash_refresh_token(refresh_token) if refresh_token else None
    with SessionLocal() as db:
        sessions = db.scalars(
            select(RefreshSession)
            .where(RefreshSession.user_id == user.id, RefreshSession.revoked_at.is_(None))
            .order_by(RefreshSession.last_used.desc())
        ).all()
        return {
            "sessions": [
                {
                    "id": session.id,
                    "user_agent": session.user_agent,
                    "created_at": session.created_at,
                    "last_used": session.last_used,
                    "current": bool(current_hash and session.refresh_hash == current_hash),
                }
                for session in sessions
            ]
        }


@router.delete("/me/sessions/{session_id}")
def revoke_session(
    session_id: int,
    request: Request,
    response: Response,
    user: User = Depends(get_current_user),
):
    if settings.is_production and not validate_csrf(request):
        raise HTTPException(403, "CSRF validation failed.") from None
    refresh_token = read_cookie(request, "refresh_token")
    current_hash = _hash_refresh_token(refresh_token) if refresh_token else None
    with SessionLocal() as db:
        session = db.scalar(
            select(RefreshSession).where(
                RefreshSession.id == session_id,
                RefreshSession.user_id == user.id,
                RefreshSession.revoked_at.is_(None),
            )
        )
        if session is None:
            raise HTTPException(404, "Session not found.") from None
        is_current = bool(current_hash and session.refresh_hash == current_hash)
        session.revoked_at = datetime.now(UTC)
        db.commit()
    if is_current:
        clear_access_token_cookie(response)
        clear_refresh_token_cookie(response)
    return _same_shape_response("Session revoked.")


@router.patch("/me")
def update_me(
    payload: ProfileUpdateRequest,
    request: Request,
    user: User = Depends(get_current_user),
):
    if settings.is_production and not validate_csrf(request):
        raise HTTPException(403, "CSRF validation failed.") from None
    with SessionLocal() as db:
        current = db.get(User, user.id)
        if current is None:
            raise HTTPException(404, "User not found.") from None
        if payload.username is not None:
            username = payload.username.strip()
            collision = db.scalar(
                select(User.id).where(
                    func.lower(User.username) == username.casefold(),
                    User.id != user.id,
                )
            )
            if collision is not None:
                raise HTTPException(409, "That username is already in use.") from None
            current.username = username
        if payload.display_name is not None:
            current.display_name = payload.display_name
        try:
            db.commit()
        except IntegrityError as exc:
            db.rollback()
            raise HTTPException(409, "That username is already in use.") from exc
        db.refresh(current)
        return _user_profile(current)


@router.get("/me/personalization")
def get_personalization_settings(user: User = Depends(get_current_user)):
    with SessionLocal() as db:
        setting = db.scalar(
            select(UserPersonalizationSetting).where(UserPersonalizationSetting.user_id == user.id)
        )
        return _personalization(setting)


@router.get("/me/onboarding")
def onboarding_options(user: User = Depends(get_current_user)):
    from app.services.preference_learning import ONBOARDING_TRAITS
    from app.services.recommendation_engine import GENRE_NAMES

    with SessionLocal() as db:
        has_explicit = (
            db.scalar(
                select(Preference.id)
                .where(Preference.user_id == user.id, Preference.source == "explicit")
                .limit(1)
            )
            is not None
        )
    return {
        "genres": sorted(set(GENRE_NAMES.values())),
        "traits": list(ONBOARDING_TRAITS),
        "has_explicit_preferences": has_explicit,
    }


@router.post("/me/onboarding")
def save_onboarding(
    payload: OnboardingRequest,
    request: Request,
    user: User = Depends(get_current_user),
):
    if settings.is_production and not validate_csrf(request):
        raise HTTPException(403, "CSRF validation failed.") from None
    from app.memory import MemoryService
    from app.services.preference_learning import apply_onboarding_preferences

    with SessionLocal() as db:
        service = MemoryService(db)
        if not service.memory_enabled(user.id):
            return {"saved": [], "memory_enabled": False}
        saved = apply_onboarding_preferences(
            service,
            user.id,
            favorite_genres=payload.favorite_genres,
            avoid_genres=payload.avoid_genres,
            avoid_traits=payload.avoid_traits,
            favorite_movies=payload.favorite_movies,
        )
    return {
        "saved": [
            {"dimension": item.dimension, "value": item.value, "polarity": item.polarity} for item in saved
        ],
        "memory_enabled": True,
    }


@router.post("/me/feedback")
async def submit_feedback(
    payload: UserFeedbackRequest,
    request: Request,
    user: User = Depends(get_current_user),
):
    if settings.is_production and not validate_csrf(request):
        raise HTTPException(403, "CSRF validation failed.") from None
    key = payload.idempotency_key or str(uuid.uuid4())

    def feedback_tx(db):
        existing = db.scalar(
            select(Feedback).where(
                Feedback.user_id == user.id,
                Feedback.idempotency_key == key,
            )
        )
        if existing is not None:
            return existing.id, False
        feedback = Feedback(
            user_id=user.id,
            feedback_type=("product" if payload.category == "general" else f"product:{payload.category}"),
            text=payload.text.strip(),
            idempotency_key=key,
        )
        db.add(feedback)
        db.flush()
        db.commit()
        return feedback.id, True

    try:
        feedback_id, created = await run_db(feedback_tx)
    except IntegrityError:
        feedback_id, created = await run_db(
            lambda db: (
                (existing.id, False)
                if (
                    existing := db.scalar(
                        select(Feedback).where(Feedback.user_id == user.id, Feedback.idempotency_key == key)
                    )
                )
                else (None, False)
            )
        )
        if feedback_id is None:
            raise HTTPException(409, "Feedback could not be recorded safely; please retry.") from None
    return {
        **_same_shape_response("Thank you for your feedback."),
        "feedback_id": feedback_id,
        "created": created,
    }


@router.patch("/me/personalization")
def update_personalization_settings(
    payload: PersonalizationUpdateRequest,
    request: Request,
    user: User = Depends(get_current_user),
):
    if settings.is_production and not validate_csrf(request):
        raise HTTPException(403, "CSRF validation failed.") from None
    if not payload.model_dump(exclude_none=True):
        raise HTTPException(400, "No personalization setting was provided.") from None
    with SessionLocal() as db:
        setting = db.scalar(
            select(UserPersonalizationSetting).where(UserPersonalizationSetting.user_id == user.id)
        )
        if setting is None:
            setting = UserPersonalizationSetting(user_id=user.id)
            db.add(setting)
        if payload.memory_enabled is not None:
            setting.memory_enabled = payload.memory_enabled
        if payload.reminders_enabled is not None:
            setting.reminders_enabled = payload.reminders_enabled
        if payload.answer_style is not None:
            setting.answer_style = payload.answer_style
        if payload.daily_brief_enabled is not None:
            setting.daily_brief_enabled = payload.daily_brief_enabled
        db.commit()
        return _personalization(setting)


@router.get("/me/ui-settings")
def get_ui_settings(request: Request, user: User = Depends(get_current_user)):
    locale_parts = request.headers.get("accept-language", "").split(",", 1)[0].replace("_", "-").split("-")
    default_region = (
        locale_parts[1].upper()
        if len(locale_parts) > 1 and len(locale_parts[1]) == 2
        else settings.default_region
    )
    with SessionLocal() as db:
        setting = db.scalar(select(UserInterfaceSetting).where(UserInterfaceSetting.user_id == user.id))
        if setting is None:
            return {
                "theme": "system",
                "density": "comfortable",
                "region": default_region,
                "motion": "system",
            }
        return {
            "theme": setting.theme,
            "density": setting.density,
            "region": setting.region,
            "motion": setting.motion or "system",
        }


@router.patch("/me/ui-settings")
def update_ui_settings(
    payload: UserInterfaceSettingsUpdateRequest,
    request: Request,
    user: User = Depends(get_current_user),
):
    if settings.is_production and not validate_csrf(request):
        raise HTTPException(403, "CSRF validation failed.") from None
    changes = payload.model_dump(exclude_unset=True)
    if not changes:
        raise HTTPException(400, "No interface settings were provided.") from None
    if changes.get("region") is not None:
        changes["region"] = changes["region"].upper()
    with SessionLocal() as db:
        setting = db.scalar(select(UserInterfaceSetting).where(UserInterfaceSetting.user_id == user.id))
        if setting is None:
            setting = UserInterfaceSetting(user_id=user.id)
            db.add(setting)
        for key, value in changes.items():
            if value is not None:
                setattr(setting, key, value)
        db.commit()
        return {
            "theme": setting.theme,
            "density": setting.density,
            "region": setting.region,
            "motion": setting.motion or "system",
        }


@router.post("/me/personalization/clear")
def clear_personalization(request: Request, user: User = Depends(get_current_user)):
    if settings.is_production and not validate_csrf(request):
        raise HTTPException(403, "CSRF validation failed.") from None
    with SessionLocal() as db:
        db.execute(delete(Memory).where(Memory.user_id == user.id))
        db.execute(delete(Preference).where(Preference.user_id == user.id))
        db.commit()
    return _same_shape_response("Personalization data cleared.")


@router.patch("/me/preferences/{preference_id}")
def update_preference(
    preference_id: int,
    payload: PreferenceUpdateRequest,
    request: Request,
    user: User = Depends(get_current_user),
):
    if settings.is_production and not validate_csrf(request):
        raise HTTPException(403, "CSRF validation failed.") from None
    changes = payload.model_dump(exclude_unset=True)
    if not changes:
        raise HTTPException(400, "No preference changes were provided.") from None
    with SessionLocal() as db:
        preference = db.scalar(
            select(Preference).where(
                Preference.id == preference_id,
                Preference.user_id == user.id,
            )
        )
        if preference is None:
            raise HTTPException(404, "Preference not found.") from None
        for field, value in changes.items():
            setattr(preference, field, value.strip() if isinstance(value, str) else value)
        # A preference the user corrected is now stated by the user, not inferred.
        preference.source = "explicit"
        preference.confidence = max(preference.confidence or 0.0, 0.9)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            raise HTTPException(409, "That preference already exists.") from None
        return {
            "id": preference.id,
            "dimension": preference.dimension,
            "value": preference.value,
            "polarity": preference.polarity,
            "strength": preference.strength,
            "confidence": preference.confidence,
            "source": preference.source,
        }


@router.delete("/me/preferences/{preference_id}")
def delete_preference(
    preference_id: int,
    request: Request,
    user: User = Depends(get_current_user),
):
    if settings.is_production and not validate_csrf(request):
        raise HTTPException(403, "CSRF validation failed.") from None
    with SessionLocal() as db:
        preference = db.scalar(
            select(Preference).where(
                Preference.id == preference_id,
                Preference.user_id == user.id,
            )
        )
        if preference is None:
            raise HTTPException(404, "Preference not found.") from None
        db.delete(preference)
        db.commit()
    return _same_shape_response("Preference deleted.")


class MemoryUpdateRequest(BaseModel):
    content: str = Field(min_length=1, max_length=2000)


@router.patch("/me/memories/{memory_id}")
def update_memory(
    memory_id: int,
    payload: MemoryUpdateRequest,
    request: Request,
    user: User = Depends(get_current_user),
):
    """Correct what Grace remembers (UI spec §81-§82)."""
    if settings.is_production and not validate_csrf(request):
        raise HTTPException(403, "CSRF validation failed.") from None
    from app.memory import memory_content_hash

    content = " ".join(payload.content.split())
    with SessionLocal() as db:
        memory = db.scalar(select(Memory).where(Memory.id == memory_id, Memory.user_id == user.id))
        if memory is None:
            raise HTTPException(404, "Memory not found.") from None
        memory.content = content
        memory.content_hash = memory_content_hash(content)
        # A memory the user wrote themselves is a stated fact, not an inference.
        memory.memory_type = "preference" if memory.memory_type == "preference" else "user_stated"
        memory.importance = max(memory.importance or 0.0, 0.8)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            raise HTTPException(409, "You already have that memory.") from None
        return {"id": memory.id, "type": memory.memory_type, "content": memory.content}


@router.delete("/me/memories/{memory_id}")
def delete_memory(
    memory_id: int,
    request: Request,
    user: User = Depends(get_current_user),
):
    if settings.is_production and not validate_csrf(request):
        raise HTTPException(403, "CSRF validation failed.") from None
    with SessionLocal() as db:
        memory = db.scalar(select(Memory).where(Memory.id == memory_id, Memory.user_id == user.id))
        if memory is None:
            raise HTTPException(404, "Memory not found.") from None
        db.delete(memory)
        db.commit()
    return _same_shape_response("Memory deleted.")


@router.post("/me/password")
async def change_password(
    request: Request,
    payload: PasswordChangeRequest,
    user: User = Depends(get_current_user),
):
    if settings.is_production and not validate_csrf(request):
        raise HTTPException(403, "CSRF validation failed.") from None
    current = await run_db(lambda db: db.get(User, user.id))
    if current is None:
        raise HTTPException(404, "User not found.") from None
    if not await async_verify_password(payload.current_password, current.password_hash):
        raise HTTPException(401, "Current password is incorrect.") from None
    ok, issue = is_password_allowed(payload.new_password)
    if not ok:
        raise HTTPException(400, issue or "Password is invalid.") from None
    await _assert_password_not_breached(payload.new_password)
    new_hash = await async_hash_password_argon2id(payload.new_password)

    def change_password_tx(db):
        current = db.get(User, user.id)
        if current is None:
            return False
        current.password_hash = new_hash
        db.execute(
            update(RefreshSession)
            .where(RefreshSession.user_id == user.id, RefreshSession.revoked_at.is_(None))
            .values(revoked_at=datetime.now(UTC), revoked_reason="password_change")
        )
        db.commit()
        return True

    if not await run_db(change_password_tx):
        raise HTTPException(404, "User not found.") from None
    return _same_shape_response("Password updated.")


@router.post("/me/email")
async def change_email(
    payload: EmailRequest,
    request: Request,
    user: User = Depends(get_current_user),
):
    if settings.is_production and not validate_csrf(request):
        raise HTTPException(403, "CSRF validation failed.") from None
    email = _normalize_email(payload.email)
    user_obj = await _get_user_by_email(email)
    if user_obj is not None and user_obj.id != user.id:
        raise HTTPException(409, "This email is already in use.") from None

    def inspect_pending(db):
        recent = db.scalar(
            select(PendingEmailChange.id).where(
                PendingEmailChange.user_id == user.id,
                PendingEmailChange.created_at >= datetime.now(UTC) - timedelta(seconds=60),
            )
        )
        pending_collision = db.scalar(
            select(PendingEmailChange.user_id).where(
                PendingEmailChange.email == email,
                PendingEmailChange.user_id != user.id,
            )
        )
        return bool(recent), pending_collision is not None

    recent, pending_collision = await run_db(inspect_pending)
    if recent:
        raise HTTPException(429, "Please wait before requesting another verification code.") from None
    if pending_collision:
        raise HTTPException(409, "This email address is already pending verification.") from None
    code = "".join(secrets.choice("0123456789") for _ in range(6))
    otp_hash = await asyncio.to_thread(hash_otp, code)

    def create_pending(db):
        db.execute(delete(PendingEmailChange).where(PendingEmailChange.user_id == user.id))
        db.add(
            PendingEmailChange(
                user_id=user.id,
                email=email,
                otp_hash=otp_hash,
                expires_at=datetime.now(UTC) + otp_ttl(),
            )
        )
        db.commit()

    try:
        await run_db(create_pending)
    except IntegrityError:
        raise HTTPException(409, "This email address is already pending verification.") from None
    await _deliver_otp_email(email, code, "email_change")
    return _same_shape_response("Verification request accepted for your new email.", status="accepted")


@router.post("/me/email/verify")
def verify_email_change(
    payload: EmailChangeVerifyRequest,
    request: Request,
    user: User = Depends(get_current_user),
):
    if settings.is_production and not validate_csrf(request):
        raise HTTPException(403, "CSRF validation failed.") from None
    with SessionLocal() as db:
        pending = db.scalar(select(PendingEmailChange).where(PendingEmailChange.user_id == user.id))
        expires_at = _utc_aware(pending.expires_at) if pending is not None else None
        if pending is None or expires_at <= datetime.now(UTC):
            raise HTTPException(400, "The verification code is invalid or expired.") from None
        if pending.attempts >= OTP_ATTEMPT_LIMIT:
            raise HTTPException(429, "Too many attempts. Request a new code.") from None
        if not verify_otp(payload.code, pending.otp_hash):
            pending.attempts += 1
            db.commit()
            raise HTTPException(400, "The verification code is invalid or expired.") from None
        collision = db.scalar(select(User.id).where(User.email == pending.email, User.id != user.id))
        if collision is not None:
            raise HTTPException(409, "This email is already in use.") from None
        account = db.get(User, user.id)
        if account is None:
            raise HTTPException(404, "Account not found.") from None
        account.email = pending.email
        account.email_verified = True
        account.status = "verified"
        db.execute(
            update(RefreshSession)
            .where(RefreshSession.user_id == user.id, RefreshSession.revoked_at.is_(None))
            .values(revoked_at=datetime.now(UTC), revoked_reason="email_change")
        )
        db.delete(pending)
        try:
            db.commit()
        except IntegrityError as exc:
            db.rollback()
            raise HTTPException(409, "This email is already in use.") from exc
        updated_email = account.email
    return {"status": "success", "email": updated_email}


@router.delete("/me")
async def delete_me(
    payload: AccountDeletionRequest,
    request: Request,
    response: Response,
    user: User = Depends(get_current_user),
):
    if settings.is_production and not validate_csrf(request):
        raise HTTPException(403, "CSRF validation failed.") from None
    if not payload.current_password:
        raise HTTPException(401, "Current password is required to delete the account.") from None
    if not await async_verify_password(payload.current_password, user.password_hash):
        raise HTTPException(401, "Current password is incorrect.") from None
    deletion_requested_at = datetime.now(UTC)

    def delete_account_tx(db):
        current = db.get(User, user.id)
        if current is None:
            return False
        db.execute(
            update(RefreshSession)
            .where(RefreshSession.user_id == user.id)
            .values(revoked_at=deletion_requested_at, revoked_reason="account_deleted")
        )
        current.deleted_at = deletion_requested_at
        current.is_active = False
        current.status = "deleted"
        db.add(AuditLog(user_id=user.id, event_type="account_deletion_requested"))
        db.commit()
        return True

    if not await run_db(delete_account_tx):
        raise HTTPException(404, "Account not found.") from None
    clear_access_token_cookie(response)
    clear_refresh_token_cookie(response)
    restore_until = deletion_requested_at + timedelta(days=settings.account_deletion_grace_days)
    restore_token = create_account_restore_token(user.id, deletion_requested_at)
    response.set_cookie(
        key="grace_restore_token",
        value=restore_token,
        httponly=True,
        secure=settings.is_production,
        samesite="lax",
        max_age=settings.account_deletion_grace_days * 24 * 60 * 60,
        path="/api/v1/auth",
    )
    return {
        "status": "success",
        "message": "Account scheduled for deletion.",
        "restore_until": restore_until.isoformat(),
    }


@router.post("/me/restore")
async def restore_me(
    payload: AccountDeletionRequest,
    request: Request,
    response: Response,
):
    if settings.is_production and not validate_csrf(request):
        raise HTTPException(403, "CSRF validation failed.") from None
    restore_token = request.cookies.get("grace_restore_token")
    claims = decode_account_restore_token(restore_token or "")
    if claims is None or not payload.current_password:
        raise HTTPException(401, "Re-authentication is required to restore the account.") from None
    user_id, token_deleted_at = claims
    account = await run_db(lambda db: db.get(User, user_id))
    if account is None or account.status != "deleted" or account.deleted_at is None:
        raise HTTPException(404, "Deleted account not found.") from None
    deleted_at = _utc_aware(account.deleted_at)
    if deleted_at is None or int(deleted_at.timestamp()) != int(token_deleted_at.timestamp()):
        raise HTTPException(401, "The restoration credential is invalid.") from None
    if deleted_at is None or datetime.now(UTC) - deleted_at > timedelta(
        days=settings.account_deletion_grace_days
    ):
        raise HTTPException(410, "The account restoration period has expired.") from None
    if not await async_verify_password(payload.current_password, account.password_hash):
        raise HTTPException(401, "Current password is incorrect.") from None

    def restore_tx(db):
        current = db.scalar(
            select(User)
            .where(
                User.id == user_id,
                User.status == "deleted",
            )
            .with_for_update()
        )
        if current is None or current.deleted_at is None:
            return False
        if int(_utc_aware(current.deleted_at).timestamp()) != int(token_deleted_at.timestamp()):
            return False
        current.status = "verified"
        current.is_active = True
        current.deleted_at = None
        db.commit()
        return True

    if not await run_db(restore_tx):
        raise HTTPException(409, "The account changed while it was being restored. Please retry.") from None
    await _issue_session_async(user_id, request, response)
    response.delete_cookie("grace_restore_token", path="/api/v1/auth")
    return {"status": "success", "message": "Account restored."}


@router.post("/me/export")
async def request_export(request: Request, user: User = Depends(get_current_user)):
    if settings.is_production and not validate_csrf(request):
        raise HTTPException(403, "CSRF validation failed.") from None

    def enqueue(db):
        existing = db.scalar(
            select(ExportJob)
            .where(
                ExportJob.user_id == user.id,
                ExportJob.status.in_(["pending", "running"]),
            )
            .order_by(ExportJob.id.desc())
            .limit(1)
        )
        if existing is not None:
            return existing.id, existing.status, existing.progress
        now = datetime.now(UTC)
        job = ExportJob(
            user_id=user.id,
            status="pending",
            stage="queued",
            progress=0,
            created_at=now,
        )
        db.add(job)
        db.commit()
        return job.id, job.status, job.progress

    job_id, status, progress = await run_db(enqueue)
    return {
        "status": "accepted",
        "job_id": job_id,
        "job_status": status,
        "progress": progress,
    }


@router.get("/me/export/{job_id}")
async def get_export_status(job_id: int, user: User = Depends(get_current_user)):
    job = await run_db(
        lambda db: db.scalar(select(ExportJob).where(ExportJob.id == job_id, ExportJob.user_id == user.id))
    )
    if job is None:
        raise HTTPException(404, "Export job not found.") from None
    payload = {
        "status": "success",
        "job_id": job.id,
        "job_status": job.status,
        "stage": job.stage,
        "progress": job.progress,
        "size_bytes": job.artifact_size,
    }
    if job.status == "ready" and job.download_token_ciphertext and job.download_expires_at:
        from app.services.otp_delivery_service import decrypt_download_token

        token = decrypt_download_token(job.download_token_ciphertext)
        payload["download_ready"] = True
        payload["download_url"] = f"/api/v1/auth/me/export/{job.id}/download?token={token}"
    else:
        payload["download_ready"] = False
    return payload


@router.get("/me/export/{job_id}/download")
def download_export(job_id: int, token: str):
    # The signed, one-use download token is the authorization mechanism so an expired
    # access token cannot break a download that was already prepared.
    token_hash = hash_download_token(token)
    with SessionLocal() as db:
        job = db.scalar(
            select(ExportJob)
            .where(
                ExportJob.id == job_id,
                ExportJob.status == "ready",
                ExportJob.download_token_hash == token_hash,
            )
            .with_for_update()
        )
        if job is None or not job.artifact_path:
            raise HTTPException(404, "Export is not ready or has expired.") from None
        if job.download_expires_at is None or _utc_aware(job.download_expires_at) <= datetime.now(UTC):
            job.status = "expired"
            db.commit()
            raise HTTPException(410, "This export has expired.") from None
        artifact_path = job.artifact_path
        job.download_token_hash = None
        db.commit()

    path = Path(artifact_path)
    if not path.is_file():
        raise HTTPException(410, "The export artifact is no longer available.") from None

    def stream_file():
        with path.open("rb") as source:
            while chunk := source.read(1024 * 1024):
                yield chunk

    return StreamingResponse(
        stream_file(),
        media_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="grace-export.zip"'},
    )


@router.post("/me/adopt-guest")
async def adopt_guest_route(request: Request, response: Response, user: User = Depends(get_current_user)):
    return await _adopt_guest_state(request, response, user.id)


async def _adopt_guest_state_best_effort(request: Request, response: Response, user_id: int) -> None:
    """Carry a guest's chat and lists into the account they just created.

    Never allowed to fail sign-in: by this point the one-time code is consumed
    and the session issued, so an exception here used to turn a successful
    verification into an error the user could never get past (every retry of
    the same code is "invalid").
    """
    if read_guest_session_id(request) is None:
        return
    try:
        await _adopt_guest_state(request, response, user_id)
    except (HTTPException, SQLAlchemyError, ValueError, TypeError, KeyError, AttributeError, OSError):
        logger.exception("guest_adoption_failed user_id=%s", user_id)


async def _adopt_guest_state(request: Request, response: Response, user_id: int):
    guest_id = read_guest_session_id(request)
    if guest_id is None:
        raise HTTPException(422, "Guest session is invalid.") from None

    watchlist = await shared_state.cache_get(f"guest-watchlist:{guest_id}", 24 * 60 * 60) or []
    watched = await shared_state.cache_get(f"guest-watched:{guest_id}", 24 * 60 * 60) or []
    interactions = await shared_state.cache_get(f"guest-interactions:{guest_id}", 24 * 60 * 60) or {}
    conversation_id = interactions.get("conversation_id") if isinstance(interactions, dict) else None
    history = (
        await shared_state.cache_get(f"guest-conversation:{guest_id}:{conversation_id}", 24 * 60 * 60)
        if conversation_id
        else []
    ) or []

    def adopt_tx(db):
        existing = db.scalar(
            select(GuestAdoption)
            .where(
                GuestAdoption.user_id == user_id,
                GuestAdoption.guest_session_id == guest_id,
            )
            .with_for_update()
        )
        if existing is not None:
            return {
                "status": "already_adopted",
                "watchlist_items": existing.watchlist_items,
                "conversations": existing.conversations,
            }

        adoption = GuestAdoption(
            user_id=user_id,
            guest_session_id=guest_id,
            created_at=datetime.now(UTC),
            completed_at=datetime.now(UTC),
        )
        db.add(adoption)
        # The unique constraint is the durable concurrency/idempotency authority.
        # The adoption row and imported state are committed together.
        db.flush()

        adopted_watchlist = 0
        adopted_watched = 0
        adopted_conversations = 0
        movie_ids = [item.get("tmdb_id") for item in watchlist if isinstance(item, dict)]
        existing_movies = (
            {
                movie.tmdb_id: movie
                for movie in db.scalars(select(Movie).where(Movie.tmdb_id.in_(movie_ids))).all()
            }
            if movie_ids
            else {}
        )
        for item in watchlist:
            tmdb_id = item.get("tmdb_id")
            title = item.get("movie_title") or item.get("title")
            if not isinstance(tmdb_id, int) or not isinstance(title, str) or not title.strip():
                continue
            movie = existing_movies.get(tmdb_id)
            if movie is None:
                movie = Movie(
                    tmdb_id=tmdb_id,
                    title=title.strip(),
                    metadata_json={"source": "guest_adoption"},
                )
                db.add(movie)
                db.flush()
                existing_movies[tmdb_id] = movie
            present = db.scalar(
                select(WatchlistItem).where(
                    WatchlistItem.user_id == user_id,
                    WatchlistItem.movie_id == movie.id,
                )
            )
            if present is None:
                db.add(WatchlistItem(user_id=user_id, movie_id=movie.id))
                adopted_watchlist += 1

        for item in watched:
            tmdb_id = item.get("tmdb_id") if isinstance(item, dict) else None
            if not isinstance(tmdb_id, int):
                continue
            movie = existing_movies.get(tmdb_id) or db.scalar(select(Movie).where(Movie.tmdb_id == tmdb_id))
            if movie is None:
                continue
            present = db.scalar(
                select(WatchedItem).where(
                    WatchedItem.user_id == user_id,
                    WatchedItem.movie_id == movie.id,
                )
            )
            if present is None:
                db.add(WatchedItem(user_id=user_id, movie_id=movie.id))
                adopted_watched += 1

        if isinstance(history, list) and history:
            conversation = Conversation(user_id=user_id, title="Imported guest conversation")
            db.add(conversation)
            db.flush()
            for item in history:
                if (
                    isinstance(item, dict)
                    and item.get("role") in {"user", "assistant"}
                    and isinstance(item.get("content"), str)
                ):
                    db.add(
                        Message(
                            conversation_id=conversation.id,
                            user_id=user_id,
                            role=item["role"],
                            content=item["content"],
                            status="complete",
                        )
                    )
            adopted_conversations = 1

        if adopted_watchlist or adopted_watched:
            db.execute(
                update(User)
                .where(User.id == user_id)
                .values(taste_profile_version=User.taste_profile_version + 1)
            )
        adoption.watchlist_items = adopted_watchlist
        adoption.watched_items = adopted_watched
        adoption.conversations = adopted_conversations
        adoption.completed_at = datetime.now(UTC)
        db.commit()
        return {
            "status": "success",
            "watchlist_items": adopted_watchlist,
            "conversations": adopted_conversations,
        }

    try:
        result = await run_db(adopt_tx)
    except IntegrityError:
        # Another worker may have committed the durable adoption row while this
        # transaction was attempting the same unique key. Resolve the winner and
        # never replay imported conversation history.
        existing = await run_db(
            lambda db: db.scalar(
                select(GuestAdoption).where(
                    GuestAdoption.user_id == user_id,
                    GuestAdoption.guest_session_id == guest_id,
                )
            )
        )
        if existing is not None:
            result = {
                "status": "already_adopted",
                "watchlist_items": existing.watchlist_items,
                "conversations": existing.conversations,
            }
        else:
            # A different unique constraint (for example a concurrent collection
            # mutation) failed. Retry the whole atomic transaction once against
            # fresh database state.
            result = await run_db(adopt_tx)

    response.delete_cookie("guest_session_id", path="/")
    await shared_state.cache_set(f"guest-watchlist:{guest_id}", [], settings.cache_max_entries, ttl=1)
    await shared_state.cache_set(f"guest-watched:{guest_id}", [], settings.cache_max_entries, ttl=1)
    await shared_state.cache_set(f"guest-interactions:{guest_id}", {}, settings.cache_max_entries, ttl=1)
    if conversation_id:
        await shared_state.cache_set(
            f"guest-conversation:{guest_id}:{conversation_id}",
            [],
            settings.cache_max_entries,
            ttl=1,
        )

    return {
        "status": result["status"],
        "message": (
            "Guest session was already adopted."
            if result["status"] == "already_adopted"
            else "Guest session adopted."
        ),
        "user_id": user_id,
        "watchlist_items": result["watchlist_items"],
        "conversations": result["conversations"],
    }
