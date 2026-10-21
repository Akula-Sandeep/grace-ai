import asyncio
import base64
import hashlib
import importlib.util
import json
import logging
import re
import time
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Literal

import httpx
from fastapi import (
    BackgroundTasks,
    Depends,
    FastAPI,
    HTTPException,
    Path,
    Query,
    Request,
    Response,
)
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import and_, func, or_, select, text, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.agent.agent import MovieAgent
from app.agent.failures import AgentFailure, FailureKind, failure_response
from app.agent.tools import MovieTools
from app.capabilities import runtime_capabilities
from app.config import settings
from app.core.prompts import GRACE_PROMPT_VERSION
from app.database import (
    Conversation,
    Memory,
    Message,
    Movie,
    MovieReference,
    Preference,
    Rating,
    SessionLocal,
    User,
    UserInterfaceSetting,
    UserPersonalizationSetting,
    WatchedItem,
    WatchlistItem,
    utcnow,
)
from app.errors import (
    SharedStateUnavailable,
    http_problem_handler,
    internal_problem_handler,
    problem_response,
    request_id_for_header,
    shared_state_unavailable_handler,
    validation_problem_handler,
)
from app.guest_session import read_guest_session_id, set_guest_cookie
from app.inference.errors import MODEL_CALL_FAILURES
from app.memory import ANSWER_CARD, MemoryService
from app.movie_state import MovieStateService
from app.observability import (
    bind_log_context,
    log_event,
    logger,
    record_first_token,
    reset_log_context,
)
from app.rag import RAGService
from app.routers.auth import router as auth_router
from app.security import (
    get_current_user,
    get_current_user_optional,
    validate_csrf,
)
from app.services.upstream_errors import (
    UpstreamError,
    UpstreamNotFound,
    UpstreamTimeout,
)
from app.shared_state import shared_state
from app.telemetry import (
    finish_chat_turn,
    initialize_telemetry,
    prometheus_metrics,
    record_http_request,
    shutdown_telemetry,
    start_chat_turn,
)

_GUEST_SESSION_TTL_SECONDS = 24 * 60 * 60


def _movie_upstream_http_error(error: Exception) -> HTTPException:
    if isinstance(error, UpstreamNotFound):
        return HTTPException(404, "The requested movie was not found.")
    if isinstance(error, (UpstreamTimeout, TimeoutError)):
        return HTTPException(504, "The movie data provider timed out.")
    return HTTPException(
        502,
        "The movie data provider is temporarily unavailable.",
        headers={"Retry-After": "5"},
    )


async def run_sync(fn):
    """Run a blocking (synchronous SQLAlchemy) callable off the event loop.

    Every route below is `async def` because it may also await the agent or
    an httpx call, but the ORM layer is synchronous. Calling it directly
    inside an async function blocks the whole event loop for every other
    concurrent request; routing it through a thread keeps the loop free.
    """
    return await asyncio.to_thread(fn)


def _with_session(fn):
    with SessionLocal() as db:
        return fn(db)


async def _compact_conversation_after_response(conversation_id: int) -> None:
    """Compact a conversation without holding a synchronous ORM session across awaits."""
    from app.services.memory import (
        SUMMARY_INTERVAL,
        SUMMARY_MESSAGE_THRESHOLD,
        SUMMARY_RAW_WINDOW,
    )

    def snapshot(db):
        conversation = db.scalar(
            select(Conversation).where(
                Conversation.id == conversation_id,
                Conversation.is_deleted.is_(False),
            )
        )
        if conversation is None:
            return None
        message_count = (
            db.scalar(
                select(func.count(Message.id)).where(
                    Message.conversation_id == conversation_id,
                    Message.is_deleted.is_(False),
                )
            )
            or 0
        )
        if message_count <= SUMMARY_MESSAGE_THRESHOLD or message_count % SUMMARY_INTERVAL != 0:
            return None
        older_messages = db.scalars(
            select(Message)
            .where(
                Message.conversation_id == conversation_id,
                Message.is_deleted.is_(False),
                Message.role.in_(("user", "assistant")),
                Message.status.in_(("complete", "stopped")),
                (
                    Message.id > conversation.summary_through_message_id
                    if conversation.summary_through_message_id is not None
                    else True
                ),
            )
            .order_by(Message.created_at.asc(), Message.id.asc())
            .limit(max(0, message_count - SUMMARY_RAW_WINDOW))
        ).all()
        if not older_messages:
            return None
        return {
            "through_id": older_messages[-1].id,
            # What the summary covered when this snapshot was taken: a concurrent
            # compaction that moved it on wins, and this one is dropped.
            "prior_through_id": conversation.summary_through_message_id,
            "prior_summary": conversation.summary or "",
            "history": "\n".join(f"{item.role}: {item.content}" for item in older_messages),
        }

    snapshot_data = await run_sync(lambda: _with_session(snapshot))
    if snapshot_data is None:
        return
    from app.inference.providers.chain import note_external_failure, resting_reason

    if resting_reason(settings.groq_summary_model):
        # Background work never competes with chat turns for a rate-limited model.
        return
    history = snapshot_data["history"]
    if len(history) > 12000:
        history = history[-12000:]
    from app.inference.models import InferenceRequest
    from app.services.memory import SUMMARY_SYSTEM_PROMPT

    try:
        summarizer = agent.router.summary() if hasattr(agent.router, "summary") else agent.router.fast()
        response = await summarizer.generate(
            InferenceRequest(
                messages=[
                    {"role": "system", "content": SUMMARY_SYSTEM_PROMPT},
                    {
                        "role": "user",
                        "content": (
                            f"Prior summary:\n{snapshot_data['prior_summary'] or '(none)'}\n\n"
                            f"Older conversation messages:\n{history}"
                        ),
                    },
                ],
                temperature=0.2,
                max_output_tokens=500,
            )
        )
        updated_summary = (response.text or "").strip()
        if not updated_summary:
            return

        def persist(db):
            current = db.scalar(
                select(Conversation).where(
                    Conversation.id == conversation_id,
                    Conversation.is_deleted.is_(False),
                )
            )
            if current is None:
                return
            if (
                current.summary_through_message_id != snapshot_data["prior_through_id"]
                or (current.summary or "") != snapshot_data["prior_summary"]
            ):
                return
            current.summary = updated_summary
            current.summary_through_message_id = snapshot_data["through_id"]
            db.commit()

        await run_sync(lambda: _with_session(persist))
    except Exception as exc:
        note_external_failure(settings.groq_summary_model, exc)
        logger.exception("conversation_compaction_failed conversation_id=%s", conversation_id)


async def _ingest_chat_memory_after_response(
    user_id: int,
    message: str,
    source_message_id: int | None = None,
    learned: dict | None = None,
) -> None:
    """Learn from a message after the reply. ``learned`` is filled by the stream: when the
    turn itself stored the stated taste ("memory" event), it is not counted a second time."""
    if source_message_id is not None:
        # Once per message: "Try again" resends the same message.
        try:
            if not await shared_state.claim(f"memory-ingested:{source_message_id}", ttl=7 * 24 * 60 * 60):
                return
        except SharedStateUnavailable:
            pass

    facts = None
    try:
        from app.inference.providers.chain import resting_reason
        from app.services.memory_extraction import extract_facts, worth_extracting

        def memory_on() -> bool:
            with SessionLocal() as session:
                return MemoryService(session).memory_enabled(user_id)

        if (
            worth_extracting(message)
            and hasattr(agent.router, "summary")
            and not resting_reason(settings.groq_summary_model)
            and await run_sync(memory_on)
        ):
            facts = await extract_facts(agent.router.summary(), message)
    except (SQLAlchemyError, *MODEL_CALL_FAILURES) as exc:  # background learning never fails loudly
        logger.warning("memory_extraction_failed user_id=%s error=%r", user_id, exc)
        facts = None

    if not facts:
        return

    def ingest() -> None:
        # Durable facts a model extracted from the message. Tastes the user stated are saved
        # during the turn by the answering model (remember_taste), never re-parsed here.
        db = SessionLocal()
        try:
            from app.services.memory_extraction import save_facts

            save_facts(MemoryService(db), user_id, facts, source_message_id)
            db.commit()
        except Exception:
            logger.exception("chat_memory_ingestion_failed user_id=%s", user_id)
            db.rollback()
        finally:
            db.close()

    await run_sync(ingest)


async def _purge_deleted_conversations_loop() -> None:
    from app.services.conversation_purge import purge_expired_conversations

    while True:
        try:
            purged = await asyncio.to_thread(lambda: _purge_with_session(purge_expired_conversations))
            if purged:
                logger.info("expired_conversations_purged count=%s", purged)
        except Exception:
            logger.exception("expired_conversation_purge_failed")
        await asyncio.sleep(24 * 60 * 60)


def _purge_with_session(purge):
    with SessionLocal() as db:
        return purge(db)


def _schema_status() -> tuple[str | None, str | None]:
    """(database revision, code head revision); None where unknown.

    A code change that ships without its migration (e.g. a new column) makes
    every query on that table fail, which surfaces to people as "login/verify
    always fails". This makes that state loud at startup and in /ready.
    """
    from pathlib import Path

    try:
        from alembic.config import Config
        from alembic.script import ScriptDirectory

        ini = Path(__file__).resolve().parent.parent / "alembic.ini"
        if not ini.exists():
            return None, None
        config = Config(str(ini))
        config.set_main_option("script_location", str(ini.parent / "alembic"))
        head = ScriptDirectory.from_config(config).get_current_head()
    except (ImportError, OSError, ValueError, KeyError):  # alembic missing in a slim image
        return None, None
    try:
        with SessionLocal() as db:
            current = db.execute(text("SELECT version_num FROM alembic_version")).scalar()
    except SQLAlchemyError:  # no alembic_version table (e.g. test databases built by create_all)
        return None, head
    return current, head


@asynccontextmanager
async def lifespan(_app):
    initialize_telemetry(_app)
    _require_production_config()
    try:
        await shared_state.check_ready()
    except Exception as exc:
        logger.exception("shared_state_startup_failed")
        raise RuntimeError("Shared state backend is unavailable.") from exc
    capabilities = runtime_capabilities()
    log_event(
        "runtime_capabilities",
        level=logging.INFO if all(capabilities.values()) else logging.WARNING,
        details=capabilities,
    )
    await run_sync(lambda: _with_session(lambda db: db.execute(text("SELECT 1 FROM users WHERE 1=0"))))
    current_revision, head_revision = await run_sync(_schema_status)
    if current_revision and head_revision and current_revision != head_revision:
        logger.error(
            "database_schema_out_of_date current=%s head=%s action=%s",
            current_revision,
            head_revision,
            "run `alembic upgrade head` (docker compose runs the db-migrate service)",
        )
    from app.services.http_client import initialize_shared_client

    await initialize_shared_client()
    agent._shared_services()
    purge_task = asyncio.create_task(_purge_deleted_conversations_loop())
    logger.info(
        "app_startup environment=%s database=%s",
        settings.environment,
        "sqlite" if settings.database_url.startswith("sqlite") else "other",
    )
    try:
        yield
    finally:
        purge_task.cancel()
        await asyncio.gather(purge_task, return_exceptions=True)
        await agent.close()
        from app.services.http_client import close_shared_client

        await close_shared_client()
        shutdown_telemetry()
        logger.info("app_shutdown")


app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    description="Persistent personal movie-intelligence agent.",
    lifespan=lifespan,
    docs_url=None if settings.is_production else "/docs",
    redoc_url=None if settings.is_production else "/redoc",
    openapi_url=None if settings.is_production else "/openapi.json",
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=(
        settings.allowed_cors_origins
        if settings.is_production
        else list(
            dict.fromkeys(
                [
                    *settings.allowed_cors_origins,
                    "http://localhost:3000",
                    "http://localhost:5173",
                ]
            )
        )
    ),
    allow_origin_regex=None if settings.is_production else settings.cors_origin_regex,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=[
        "Content-Type",
        "Accept",
        "Authorization",
        "X-CSRF-Token",
        "X-Request-ID",
    ],
    expose_headers=["X-Access-Token-Expires-At", "X-CSRF-Token", "X-Problem-Code", "X-Request-ID"],
)

app.add_exception_handler(SharedStateUnavailable, shared_state_unavailable_handler)
app.add_exception_handler(StarletteHTTPException, http_problem_handler)
app.add_exception_handler(RequestValidationError, validation_problem_handler)
app.add_exception_handler(Exception, internal_problem_handler)
app.include_router(auth_router, prefix="/api/v1")


class RequestDeadlineExceeded(TimeoutError):
    pass


async def _pump_agent_stream(stream, queue: asyncio.Queue) -> None:
    """Iterate an agent stream in a single task and forward its items."""
    try:
        async for delta in stream:
            await queue.put(("chunk", delta))
        from app.agent.dispatcher import get_stream_metadata

        await queue.put(("end", get_stream_metadata()))
    except asyncio.CancelledError:
        await stream.aclose()
        raise
    except Exception as exc:  # forwarded to the SSE consumer, which classifies it
        await queue.put(("error", exc))


def _sse_event(event_name: str, payload: dict) -> str:
    return f"event: {event_name}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


@app.middleware("http")
async def request_context(request: Request, call_next):
    """Attach a request id to every request/response and log unhandled
    exceptions with it, instead of letting them vanish into a bare 502."""
    request_id = request_id_for_header(request.headers.get("X-Request-ID"))
    request.state.request_id = request_id
    request.state.deadline = asyncio.get_running_loop().time() + settings.agent_deadline_seconds
    start = time.monotonic()
    try:
        async with asyncio.timeout_at(request.state.deadline):
            response = await call_next(request)
    except TimeoutError:
        duration = time.monotonic() - start
        record_http_request(request.method, "deadline_exceeded", 504, duration)
        return problem_response(
            request,
            status=504,
            code="deadline_exceeded",
            title="Request deadline exceeded",
            detail="The request exceeded its time budget.",
        )
    except Exception:
        duration = time.monotonic() - start
        route = request.scope.get("route")
        record_http_request(
            request.method,
            getattr(route, "path", "unmatched"),
            500,
            duration,
        )
        logger.exception(
            "unhandled_request_error request_id=%s method=%s path=%s",
            request_id,
            request.method,
            request.url.path,
        )
        return problem_response(
            request,
            status=500,
            code="internal_error",
            title="Internal server error",
            detail="The request could not be completed.",
        )
    response.headers["X-Request-ID"] = request_id
    duration = time.monotonic() - start
    route = request.scope.get("route")
    record_http_request(
        request.method,
        getattr(route, "path", "unmatched"),
        response.status_code,
        duration,
    )
    logger.info(
        "request method=%s path=%s status=%s duration_ms=%.1f request_id=%s",
        request.method,
        request.url.path,
        response.status_code,
        duration * 1000,
        request_id,
    )
    return response


@app.middleware("http")
async def csrf_protection(request: Request, call_next):
    if (
        settings.is_production
        and request.method in {"POST", "PUT", "PATCH", "DELETE"}
        and request.url.path != "/api/v1/auth/csrf"
        and not validate_csrf(request)
    ):
        return problem_response(
            request,
            status=403,
            code="csrf_failed",
            title="CSRF validation failed",
            detail="A valid CSRF token and allowed origin are required.",
        )
    return await call_next(request)


@app.get("/metrics", include_in_schema=False)
async def metrics(request: Request):
    return await prometheus_metrics(request)


@app.middleware("http")
async def remove_server_header(_request: Request, call_next):
    response = await call_next(_request)
    if "server" in response.headers:
        del response.headers["server"]
    return response


def _require_production_config():
    # Previously this only ran when database_url wasn't sqlite, which meant a
    # SQLite-backed "production" deployment silently skipped every check.
    # `environment` is now the explicit, unambiguous switch.
    if not settings.is_production:
        return
    if not settings.database_url.startswith(("postgresql://", "postgresql+psycopg://")):
        raise RuntimeError("Production requires PostgreSQL; SQLite is supported only by test fixtures.")
    required_security = {
        "JWT_SIGNING_KEY": settings.jwt_signing_key or settings.jwt_secret,
        "OTP_PEPPER": settings.otp_pepper,
        "CSRF_KEY": settings.csrf_key,
        "METRICS_SALT": settings.metrics_salt or settings.metrics_user_salt,
        "REDIS_URL": settings.redis_url,
        "METRICS_BEARER_TOKEN": settings.metrics_bearer_token,
    }
    missing_security = [
        name for name, value in required_security.items() if not value or len(str(value).strip()) < 32
    ]
    if missing_security:
        raise RuntimeError("Production security configuration is incomplete: " + ", ".join(missing_security))
    missing = [
        name
        for name, value in {
            "TMDB_API_KEY": settings.tmdb_api_key,
            "GROQ_API_KEY": settings.groq_api_key,
            "GEMINI_API_KEY": settings.gemini_api_key,
            "SMTP_EMAIL": settings.smtp_user or settings.smtp_email,
            "SMTP_PASSWORD": settings.smtp_password,
        }.items()
        if not value
    ]
    if missing:
        raise RuntimeError("Required production integrations are not configured: " + ", ".join(missing))


agent = MovieAgent()


def _movie_tools(db=None, db_factory=None) -> MovieTools:
    """Reuse shared TMDB/web clients and isolate sync DB work from async routes."""
    tmdb, web = agent._shared_services()
    return MovieTools(tmdb=tmdb, web=web, db=db, db_factory=db_factory)


def _load_user_profile(user_id: int):
    with SessionLocal() as db:
        return MemoryService(db).profile(user_id)


async def _load_user_profile_async(user_id: int):
    return await run_sync(lambda: _load_user_profile(user_id))


async def _enforce_chat_rate_limits(request: Request | None, user_id: int | str | None) -> None:
    client = getattr(request, "client", None)
    client_host = client.host if client else "unknown"
    if not await shared_state.rate_limit(f"chat:{user_id}", settings.chat_rate_limit):
        raise HTTPException(429, "Too many requests. Try again shortly.") from None
    if not await shared_state.rate_limit(f"chat-ip:{client_host}", settings.chat_ip_rate_limit):
        raise HTTPException(429, "Too many requests from this IP. Try again shortly.") from None
    if request is not None and not isinstance(user_id, int):
        fingerprint = hashlib.sha256(
            "|".join(
                (
                    request.headers.get("user-agent", ""),
                    request.headers.get("accept-language", ""),
                )
            ).encode()
        ).hexdigest()[:24]
        if not await shared_state.rate_limit(f"guest-fingerprint:{fingerprint}", 30, 60):
            raise HTTPException(429, "Guest usage is temporarily limited. Sign in to continue.") from None
        if not await shared_state.rate_limit("guest-global-daily", 10000, 24 * 60 * 60):
            raise HTTPException(429, "Guest usage is temporarily limited. Sign in to continue.") from None


# Requests per minute (signed in, guest) and per day for guests, by kind of page data.
# Opening one film loads about ten of these at once (details, intelligence categories,
# visuals, reviews, similar, recommendations) and search runs as the user types, so
# one shared bucket of 10/minute blocked ordinary browsing (owner, 2026-10-21).
_RATE_GROUPS = {
    "search": (120, 60, 2000),
    "browse": (120, 60, 1500),
    "model": (10, 5, 30),  # a model call per request (visual comparison)
}


async def _enforce_expensive_rate_limit(request: Request, user: User | None, group: str = "browse") -> None:
    per_user, per_guest, guest_daily = _RATE_GROUPS[group]
    if user is not None:
        key = f"{group}:user:{user.id}"
        limit = per_user
    else:
        guest_id = read_guest_session_id(request)
        fingerprint = guest_id or (request.client.host if request.client else "unknown")
        key = f"{group}:guest:{fingerprint}"
        limit = per_guest
    if not await shared_state.rate_limit(key, limit, 60):
        raise HTTPException(
            429,
            "This feature is temporarily rate-limited. Please wait a moment.",
            headers={"Retry-After": "60"},
        ) from None
    if user is None and not await shared_state.rate_limit(f"{key}:daily", guest_daily, 24 * 60 * 60):
        raise HTTPException(
            429,
            "Guest daily limit reached. Sign in to continue.",
            headers={"Retry-After": "3600"},
        ) from None


class ChatIn(BaseModel):
    message: str = Field(min_length=1, max_length=12000)
    conversation_id: int | uuid.UUID | None = None
    client_message_id: uuid.UUID | None = None
    parent_id: uuid.UUID | None = None
    regenerate_of: uuid.UUID | None = None
    continue_from: uuid.UUID | None = None
    edit_message_id: uuid.UUID | None = None
    timezone: str | None = Field(default=None, max_length=64)
    images: list[str] = Field(default_factory=list, max_length=3)
    # Start a new chat inside this project (ignored for an existing chat and for guests).
    project_id: int | None = None

    @field_validator("images")
    @classmethod
    def _valid_images(cls, value: list[str]) -> list[str]:
        from app.services.user_images import validate_image_data_url

        return [validate_image_data_url(image) for image in value]


class ChatOut(BaseModel):
    response: str
    conversation_id: int | str | None


class StreamStartedEvent(BaseModel):
    event: Literal["started"] = "started"
    conversation_id: int | str
    message_id: uuid.UUID


class StreamStatusEvent(BaseModel):
    event: Literal["status"] = "status"
    stage: str
    label: str


class StreamProvidersEvent(BaseModel):
    event: Literal["providers"] = "providers"
    providers: dict


class StreamStateEvent(BaseModel):
    """The user's lists changed during this turn (an action from chat); refresh them."""

    event: Literal["state"] = "state"
    changed: list[Literal["watchlist", "watched", "ratings", "reminders"]]
    tmdb_ids: list[int] = Field(default_factory=list)


class StreamSource(BaseModel):
    url: str
    title: str
    domain: str
    kind: Literal["web", "structured"] = "web"
    tier: int = 0


class StreamSourcesEvent(BaseModel):
    event: Literal["sources"] = "sources"
    sources: list[StreamSource]


class StreamTokenEvent(BaseModel):
    event: Literal["token"] = "token"
    text: str


class StreamDoneEvent(BaseModel):
    event: Literal["done"] = "done"
    message_id: uuid.UUID
    usage: dict


class StreamErrorEvent(BaseModel):
    event: Literal["error"] = "error"
    code: str
    message: str
    retryable: bool


def _inline_json_schema(model: type[BaseModel]) -> dict:
    """Return a model schema with nested ``$defs`` inlined.

    The SSE schema is embedded directly in a response object, where a
    ``#/$defs/...`` reference has nothing to resolve against.
    """
    schema = model.model_json_schema()
    definitions = schema.pop("$defs", {})

    def resolve(node):
        if isinstance(node, dict):
            ref = node.get("$ref")
            if isinstance(ref, str) and ref.startswith("#/$defs/"):
                return resolve(definitions[ref.removeprefix("#/$defs/")])
            return {key: resolve(value) for key, value in node.items()}
        if isinstance(node, list):
            return [resolve(item) for item in node]
        return node

    return resolve(schema)


CHAT_STREAM_RESPONSES = {
    200: {
        "description": "Server-Sent Events using the typed chat stream contract.",
        "content": {
            "text/event-stream": {
                "schema": {
                    "oneOf": [
                        _inline_json_schema(model)
                        for model in (
                            StreamStartedEvent,
                            StreamStatusEvent,
                            StreamProvidersEvent,
                            StreamStateEvent,
                            StreamSourcesEvent,
                            StreamTokenEvent,
                            StreamDoneEvent,
                            StreamErrorEvent,
                        )
                    ]
                }
            }
        },
    }
}


class ConversationRenameIn(BaseModel):
    title: str = Field(min_length=1, max_length=200)


class IngestIn(BaseModel):
    text: str = Field(min_length=1, max_length=50000)
    source: str = Field(min_length=1, max_length=100)
    document_type: str = "article"
    movie_id: int | None = None
    author: str | None = None
    url: str | None = None


class RatingIn(BaseModel):
    rating: float = Field(ge=0, le=10)


class FeedbackIn(BaseModel):
    feedback_type: str = Field(min_length=1, max_length=80)
    text: str = Field(min_length=1, max_length=5000)
    idempotency_key: str | None = Field(default=None, min_length=8, max_length=128)


user_dep = get_current_user
optional_user_dep = get_current_user_optional


_set_guest_cookie = set_guest_cookie


async def _guest_session_id(request: Request, response: Response | None) -> str:
    session_id = read_guest_session_id(request)
    if session_id is None:
        session_id = str(uuid.uuid4())
        if response is not None:
            _set_guest_cookie(response, session_id)
    await shared_state.cache_set(
        f"guest-session:{session_id}",
        {"last_seen": datetime.now(UTC).isoformat()},
        settings.cache_max_entries,
        ttl=_GUEST_SESSION_TTL_SECONDS,
    )
    return session_id


async def _guest_conversation(session_id: str | None, requested_id: int | str | uuid.UUID | None):
    if requested_id is None:
        conversation_id = str(uuid.uuid4())
        history = []
        await shared_state.cache_set(
            f"guest-conversation:{session_id}:{conversation_id}",
            history,
            settings.cache_max_entries,
            ttl=_GUEST_SESSION_TTL_SECONDS,
        )
    else:
        try:
            conversation_id = str(uuid.UUID(str(requested_id)))
        except ValueError:
            raise HTTPException(404, "Conversation not found.") from None
        history = await shared_state.cache_get(
            f"guest-conversation:{session_id}:{conversation_id}",
            _GUEST_SESSION_TTL_SECONDS,
        )
        if not isinstance(history, list):
            raise HTTPException(404, "Conversation not found.") from None
    return conversation_id, history


async def _store_guest_turn(
    session_id: str,
    conversation_id: str,
    history: list,
    message: str,
    answer: str,
    *,
    client_message_id: str | None = None,
    assistant_message_id: str | None = None,
    turn_state: dict | None = None,
) -> None:
    key = f"guest-conversation:{session_id}:{conversation_id}"
    # Only the latest reply needs its working state; older entries stay plain.
    history = [
        {k: v for k, v in entry.items() if k != "turn_state"} for entry in history if isinstance(entry, dict)
    ]
    reply: dict = {"role": "assistant", "content": answer}
    if turn_state:
        # The guest's working state travels with their last reply, like a
        # signed-in user's does with the stored message.
        reply["turn_state"] = turn_state
    history.extend([{"role": "user", "content": message}, reply])
    history = history[-30:]
    await shared_state.cache_set(
        key,
        history,
        settings.cache_max_entries,
        ttl=_GUEST_SESSION_TTL_SECONDS,
    )
    await shared_state.cache_set(
        f"guest-interactions:{session_id}",
        {"conversation_id": conversation_id, "message_count": len(history)},
        settings.cache_max_entries,
        ttl=_GUEST_SESSION_TTL_SECONDS,
    )
    if client_message_id and assistant_message_id:
        await shared_state.cache_set(
            f"guest-idempotency:{session_id}:{client_message_id}",
            {
                "conversation_id": conversation_id,
                "answer": answer,
                "message_id": assistant_message_id,
            },
            settings.cache_max_entries,
            ttl=_GUEST_SESSION_TTL_SECONDS,
        )


async def start_guest_session(request: Request, response: Response):
    session_id = await _guest_session_id(request, response)
    return {"guest_session_id": session_id}


async def get_guest_conversation(conversation_id: str, request: Request):
    session_id = read_guest_session_id(request)
    try:
        conversation_id = str(uuid.UUID(conversation_id))
    except ValueError:
        raise HTTPException(404, "Conversation not found.") from None
    if session_id is None:
        raise HTTPException(404, "Conversation not found.") from None
    history = await shared_state.cache_get(
        f"guest-conversation:{session_id}:{conversation_id}",
        _GUEST_SESSION_TTL_SECONDS,
    )
    if not isinstance(history, list):
        raise HTTPException(404, "Conversation not found.") from None
    messages = []
    parent_id = None
    for index, item in enumerate(history):
        if not isinstance(item, dict):
            continue
        role = item.get("role")
        content = item.get("content")
        if role not in {"user", "assistant"} or not isinstance(content, str):
            continue
        message_id = str(
            uuid.uuid5(
                uuid.NAMESPACE_URL,
                f"grace-guest:{session_id}:{conversation_id}:{index}:{role}",
            )
        )
        messages.append(
            {
                "id": message_id,
                "parent_id": parent_id,
                "role": role,
                "content": content,
                "status": "complete",
                **(
                    _followups(item["turn_state"], "", signed_in=False)
                    if role == "assistant" and isinstance(item.get("turn_state"), dict)
                    else {}
                ),
            }
        )
        parent_id = message_id
    return {"id": conversation_id, "messages": messages}


async def live():
    return {"status": "alive", "timestamp": datetime.now(UTC).isoformat()}


async def health():
    def check_database():
        with SessionLocal() as db:
            db.execute(text("SELECT 1"))

    try:
        await run_sync(check_database)
    except Exception as exc:
        logger.warning("health_check_database_failed error=%r", exc)
        raise HTTPException(status_code=503, detail="Database unhealthy") from exc
    return {
        "status": "healthy",
        "database": "connected",
        "version": settings.app_version,
        "timestamp": datetime.now(UTC).isoformat(),
    }


async def ai_check_v1(u=Depends(user_dep)):
    """Call each outside service once, now (Settings → Help → Run a live check)."""
    from app.services.diagnostics import run_checks

    # Each check spends real provider quota (about ten small calls), so it is
    # bounded per account and across all accounts, not only debounced.
    try:
        allowed = await shared_state.claim(f"ai-check:{u.id}", ttl=20)
        within_limits = (
            allowed
            and await shared_state.rate_limit(f"ai-check-hour:{u.id}", 6, window=3600)
            and await shared_state.rate_limit("ai-check-hour:all", 30, window=3600)
        )
    except SharedStateUnavailable:
        allowed, within_limits = True, True
    if not allowed:
        raise HTTPException(429, "A live check just ran. Try again in a few seconds.")
    if not within_limits:
        raise HTTPException(429, "The live check has run several times this hour. Try again later.")
    return await run_checks()


def ai_status_v1(u=Depends(user_dep)):
    """Recent outcome per AI model (Settings → Help). No keys, prompts or user data."""
    from app.services.provider_health import recent_turns, snapshot
    from app.services.resilience import snapshots as breakers

    models = snapshot()
    return {
        "models": models,
        # How recent answers were produced (AI model, movie data, or failed) and
        # which services are currently resting after failures.
        "recent_turns": recent_turns(),
        "services": breakers(),
        # Whether memory and knowledge search can match by meaning, or only by words.
        "capabilities": {
            "semantic_search": importlib.util.find_spec("sentence_transformers") is not None,
        },
        "summary": (
            "unknown"
            if not models
            else (
                "ok"
                if any(row["state"] == "ok" and row["provider"] == "groq" for row in models)
                else "degraded"
            )
        ),
    }


def ready():
    if settings.is_production and not settings.has_configured_jwt_secret:
        raise HTTPException(503, "Production configuration is incomplete.") from None
    db = SessionLocal()
    try:
        db.execute(text("SELECT 1"))
    except Exception:
        logger.exception("readiness_db_failed")
        raise HTTPException(503, "Database is not ready.") from None
    finally:
        db.close()
    missing = [
        name
        for name, value in {
            "TMDB_API_KEY": settings.tmdb_api_key,
            "GEMINI_API_KEY": settings.gemini_api_key,
            "GROQ_API_KEY": settings.groq_api_key,
            "JWT_SECRET": (settings.jwt_secret if settings.has_configured_jwt_secret else ""),
        }.items()
        if not value
    ]
    if missing:
        raise HTTPException(503, "Required integrations are not configured.") from None
    if not shared_state.ready:
        raise HTTPException(503, "Shared state backend is not ready.") from None
    current_revision, head_revision = _schema_status()
    if current_revision and head_revision and current_revision != head_revision:
        raise HTTPException(503, "Database schema is out of date; run `alembic upgrade head`.") from None
    return {"status": "ready", "version": settings.app_version}


async def chat_v1(
    x: ChatIn,
    u=Depends(optional_user_dep),
    request: Request = None,
    response: Response = None,
    background_tasks: BackgroundTasks = None,
):
    return await chat(x, u, request, response, background_tasks)


async def chat(
    x: ChatIn,
    u=Depends(optional_user_dep),
    request: Request = None,
    response: Response = None,
    background_tasks: BackgroundTasks = None,
):
    guest_id = None if u else await _guest_session_id(request, response)
    user_id = u.id if u else None
    await _enforce_chat_rate_limits(request, user_id or guest_id)
    log_event(
        "chat_request",
        details={"prompt_version": GRACE_PROMPT_VERSION},
        user_id=user_id or guest_id,
    )

    guest_history = None
    if u:
        try:
            conversation_id = int(x.conversation_id) if x.conversation_id is not None else None
        except (ValueError, TypeError):
            raise HTTPException(404, "Conversation not found.") from None
    else:
        conversation_id, guest_history = await _guest_conversation(guest_id, x.conversation_id)
    if u and conversation_id is None:

        def _create_conversation():
            db = SessionLocal()
            try:
                conversation = Conversation(
                    user_id=user_id, project_id=_project_for_new_chat(db, user_id, x.project_id)
                )
                db.add(conversation)
                db.commit()
                db.refresh(conversation)
                return conversation.id
            finally:
                db.close()

        conversation_id = await run_sync(_create_conversation)

    try:
        if guest_id:
            r = await agent.run(
                x.message,
                user_id,
                conversation_id,
                guest_history=guest_history,
                guest_session_id=guest_id,
            )
        else:
            r = await agent.run(x.message, user_id, conversation_id)
    except ValueError as e:
        raise HTTPException(404, str(e)) from e
    except TimeoutError as e:
        raise HTTPException(
            504,
            "The movie agent exceeded its time budget. Please retry with a narrower request.",
        ) from e
    except Exception:
        logger.exception(
            "chat_failed user_id=%s guest_session_id=%s conversation_id=%s",
            user_id,
            guest_id,
            conversation_id,
        )
        raise HTTPException(502, "The movie agent could not complete the request safely.") from None

    if guest_id:
        await _store_guest_turn(guest_id, conversation_id, guest_history, x.message, r)
    elif background_tasks is not None:
        background_tasks.add_task(_compact_conversation_after_response, conversation_id)
        background_tasks.add_task(_ingest_chat_memory_after_response, user_id, x.message)

    # agent.run validates ownership and persists the answer. Avoid a second
    # round-trip solely to re-read the id that is already known here.
    return ChatOut(response=r, conversation_id=conversation_id)


async def chat_stream_v1(
    x: ChatIn,
    u=Depends(optional_user_dep),
    request: Request = None,
    response: Response = None,
    background_tasks: BackgroundTasks = None,
):
    return await chat_stream(x, u, request, response, background_tasks)


def _prepare_persisted_turn(user_id: int, payload: ChatIn):
    if payload.conversation_id is None:
        requested_conversation_id = None
    elif isinstance(payload.conversation_id, int):
        requested_conversation_id = payload.conversation_id
    else:
        raise HTTPException(404, "Conversation not found.") from None
    client_message_id = str(payload.client_message_id or uuid.uuid4())
    with SessionLocal() as db:
        conversation = None
        if requested_conversation_id is not None:
            conversation = db.scalar(
                select(Conversation).where(
                    Conversation.id == requested_conversation_id,
                    Conversation.user_id == user_id,
                    Conversation.is_deleted.is_(False),
                )
            )
            if conversation is None:
                raise HTTPException(404, "Conversation not found.") from None
        elif payload.regenerate_of or payload.edit_message_id:
            raise HTTPException(422, "Regeneration and editing require a conversation.") from None
        else:
            conversation = Conversation(
                user_id=user_id, project_id=_project_for_new_chat(db, user_id, payload.project_id)
            )
            db.add(conversation)
            db.flush()

        stopped_answer = None
        if payload.continue_from:
            stopped_answer = db.scalar(
                select(Message).where(
                    Message.uuid_id == str(payload.continue_from),
                    Message.conversation_id == conversation.id,
                    Message.user_id == user_id,
                    Message.role == "assistant",
                    Message.status == "stopped",
                )
            )
            user_message = (
                db.scalar(
                    select(Message).where(
                        Message.uuid_id == stopped_answer.parent_id,
                        Message.conversation_id == conversation.id,
                        Message.user_id == user_id,
                        Message.role == "user",
                    )
                )
                if stopped_answer is not None
                else None
            )
            if user_message is None:
                raise HTTPException(404, "Stopped message not found.") from None
        elif payload.regenerate_of:
            user_message = db.scalar(
                select(Message).where(
                    Message.uuid_id == str(payload.regenerate_of),
                    Message.conversation_id == conversation.id,
                    Message.user_id == user_id,
                    Message.role == "user",
                )
            )
            if user_message is None:
                raise HTTPException(404, "Message not found.") from None
        else:
            user_message = db.scalar(
                select(Message).where(
                    Message.user_id == user_id,
                    Message.client_message_id == client_message_id,
                    Message.role == "user",
                )
            )
            if user_message is not None:
                conversation = db.get(Conversation, user_message.conversation_id)
                if conversation is None or conversation.is_deleted:
                    raise HTTPException(404, "Conversation not found.") from None
                _refuse_if_already_answering(db, user_message)
                existing_answer = _finished_answer(db, conversation, user_message)
                if existing_answer is not None and existing_answer.status == "complete":
                    return conversation.id, user_message, existing_answer, True
            else:
                parent_uuid = str(payload.parent_id) if payload.parent_id else None
                if payload.edit_message_id:
                    edited = db.scalar(
                        select(Message).where(
                            Message.uuid_id == str(payload.edit_message_id),
                            Message.conversation_id == conversation.id,
                            Message.user_id == user_id,
                            Message.role == "user",
                        )
                    )
                    if edited is None:
                        raise HTTPException(404, "Message not found.") from None
                    parent_uuid = edited.parent_id
                if parent_uuid:
                    parent = db.scalar(
                        select(Message).where(
                            Message.uuid_id == parent_uuid,
                            Message.conversation_id == conversation.id,
                            Message.user_id == user_id,
                        )
                    )
                    if parent is None:
                        raise HTTPException(404, "Parent message not found.") from None
                elif not payload.edit_message_id:
                    parent = db.scalar(
                        select(Message)
                        .where(
                            Message.conversation_id == conversation.id,
                            Message.user_id == user_id,
                            Message.status.in_(("complete", "stopped")),
                        )
                        .order_by(Message.created_at.desc(), Message.id.desc())
                        .limit(1)
                    )
                    parent_uuid = parent.uuid_id if parent else None
                user_message = Message(
                    client_message_id=client_message_id,
                    parent_id=parent_uuid,
                    conversation_id=conversation.id,
                    user_id=user_id,
                    role="user",
                    content=payload.message.strip(),
                    status="complete",
                )
                db.add(user_message)
                try:
                    db.flush()
                except IntegrityError:
                    db.rollback()
                    user_message = db.scalar(
                        select(Message).where(
                            Message.user_id == user_id,
                            Message.client_message_id == client_message_id,
                            Message.role == "user",
                        )
                    )
                    if user_message is None:
                        raise
                    conversation = db.get(Conversation, user_message.conversation_id)
                    if conversation is None or conversation.is_deleted:
                        raise HTTPException(404, "Conversation not found.") from None
                    _refuse_if_already_answering(db, user_message)
                    existing_answer = _finished_answer(db, conversation, user_message)
                    if existing_answer is not None and existing_answer.status == "complete":
                        return conversation.id, user_message, existing_answer, True

        assistant = Message(
            parent_id=(stopped_answer.uuid_id if stopped_answer is not None else user_message.uuid_id),
            conversation_id=conversation.id,
            user_id=user_id,
            role="assistant",
            content="",
            status="streaming",
        )
        conversation.updated_at = utcnow()
        db.add(assistant)
        db.commit()
        db.refresh(user_message)
        db.refresh(assistant)
        return conversation.id, user_message, assistant, False


def _finished_answer(db, conversation: Conversation, user_message: Message) -> Message | None:
    return db.scalar(
        select(Message)
        .where(
            Message.conversation_id == conversation.id,
            Message.parent_id == user_message.uuid_id,
            Message.role == "assistant",
            Message.status.in_(("complete", "stopped")),
        )
        .order_by(Message.created_at.desc(), Message.id.desc())
        .limit(1)
    )


def _refuse_if_already_answering(db, user_message: Message) -> None:
    """A second copy of a message that is still being answered (double send, network retry)
    must not start a second turn: tools would run twice (two reminders) and tokens be spent twice."""
    recent = utcnow() - timedelta(seconds=settings.agent_deadline_seconds + 5)
    answering = db.scalar(
        select(Message.id)
        .where(
            Message.parent_id == user_message.uuid_id,
            Message.role == "assistant",
            Message.status == "streaming",
            Message.created_at >= recent,
        )
        .limit(1)
    )
    # The first copy commits its user message and its streaming reply together,
    # so a copy that lost the insert race sees the reply here.
    if answering is not None:
        raise HTTPException(
            409,
            "Grace is still answering this message.",
            headers={"X-Problem-Code": "already_answering"},
        ) from None


def _fallback_conversation_title(message: str) -> str:
    words = re.findall(r"[\w'-]+", message.strip())
    words = words[:6]
    defaults = ("Movie", "Chat", "Conversation")
    for fallback_word in defaults:
        if len(words) >= 3:
            break
        words.append(fallback_word)
    return " ".join(words)


async def _generate_conversation_title(conversation_id: int, prompt: str, fallback: str) -> None:
    def title_generation_is_allowed() -> bool:
        with SessionLocal() as db:
            conversation = db.get(Conversation, conversation_id)
            if conversation is None or conversation.is_deleted or conversation.title_user_set:
                return False
            return (
                db.scalar(
                    select(Message.id)
                    .where(
                        Message.conversation_id == conversation_id,
                        Message.role == "assistant",
                        Message.status == "complete",
                    )
                    .limit(1)
                )
                is not None
            )

    if not await run_sync(title_generation_is_allowed):
        return

    try:
        from app.inference.models import InferenceRequest

        if getattr(agent.router.fast(), "all_resting", lambda: False)():
            raise RuntimeError("Title model resting; using the fallback title.")
        result = await agent.router.fast().generate(
            InferenceRequest(
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "Create a concise chat title of 3 to 6 words. Return only the title, "
                            "with no quotation marks or punctuation."
                        ),
                    },
                    {"role": "user", "content": prompt},
                ],
                temperature=0.2,
                max_output_tokens=24,
                reasoning_level="low",
            )
        )
        title = " ".join(re.findall(r"\S+", (result.text or "").strip())).strip(" .!?\"'")
        if not 3 <= len(title.split()) <= 6:
            title = fallback
    except Exception:
        logger.exception("conversation_title_generation_failed conversation_id=%s", conversation_id)
        title = fallback

    def save_title():
        with SessionLocal() as db:
            conversation = db.get(Conversation, conversation_id)
            completed_reply = db.scalar(
                select(Message.id)
                .where(
                    Message.conversation_id == conversation_id,
                    Message.role == "assistant",
                    Message.status == "complete",
                )
                .limit(1)
            )
            if (
                conversation is None
                or conversation.is_deleted
                or conversation.title_user_set
                or completed_reply is None
            ):
                return
            conversation.title = title
            db.commit()

    await run_sync(save_title)


def _record_turn(turn, final_status: str, metadata: dict | None) -> None:
    """One structured record per chat turn: how it was understood and answered."""
    from app.services.provider_health import record_turn

    metadata = metadata or {}
    provider = metadata.get("provider")
    if final_status == "replayed":
        source = "replay"
    elif final_status == "error":
        source = "error"
    elif provider == "evidence":
        source = "movie_data"
    elif provider == "gemini":
        source = "gemini"
    elif provider:
        source = "ai_model"
    else:
        source = "deterministic"
    turn.answer_source = source
    turn.model = metadata.get("model") if provider and provider != "evidence" else None
    summary = {**turn.summary(), "status": final_status}
    record_turn(summary)
    log_event("turn_completed", details=summary, user_id=turn.user_id or turn.guest_session_id)


async def chat_stream(
    x: ChatIn,
    u=Depends(optional_user_dep),
    request: Request = None,
    response: Response = None,
    background_tasks: BackgroundTasks = None,
):
    guest_id = None if u else await _guest_session_id(request, response)
    user_id = u.id if u else None
    await _enforce_chat_rate_limits(request, user_id or guest_id)
    log_event(
        "chat_request",
        details={"prompt_version": GRACE_PROMPT_VERSION},
        user_id=user_id or guest_id,
    )
    guest_history = None
    user_message = None
    assistant_message = None
    replay = False
    guest_replay = None
    if u:
        conversation_id, user_message, assistant_message, replay = await run_sync(
            lambda: _prepare_persisted_turn(u.id, x)
        )
        user_id = u.id
        continue_from_message_id = None
        if x.continue_from:
            continue_from_message_id = await run_sync(
                lambda: _with_session(
                    lambda db: (lambda stopped: stopped.id if stopped else None)(
                        db.scalar(select(Message).where(Message.uuid_id == str(x.continue_from)))
                    )
                )
            )
    else:
        if x.client_message_id:
            guest_replay = await shared_state.cache_get(
                f"guest-idempotency:{guest_id}:{x.client_message_id}",
                _GUEST_SESSION_TTL_SECONDS,
            )
            # As for signed-in users: a copy of a message still being answered
            # must not start a second turn.
            if guest_replay is None and not await shared_state.claim(
                f"guest-answering:{guest_id}:{x.client_message_id}",
                ttl=int(settings.agent_deadline_seconds) + 5,
            ):
                raise HTTPException(
                    409,
                    "Grace is still answering this message.",
                    headers={"X-Problem-Code": "already_answering"},
                ) from None
        guest_conversation_request = (
            guest_replay["conversation_id"] if isinstance(guest_replay, dict) else x.conversation_id
        )
        conversation_id, guest_history = await _guest_conversation(
            guest_id,
            guest_conversation_request,
        )
        assistant_message = SimpleNamespace(
            uuid_id=(guest_replay["message_id"] if isinstance(guest_replay, dict) else str(uuid.uuid4())),
            content=(guest_replay.get("answer", "") if isinstance(guest_replay, dict) else ""),
        )

    from app.agent.turn import TurnContext

    # One record for this turn, shared with the agent: the stable request id
    # (retries and regenerations keep it, so actions run once), and what the
    # agent hands back to persist (working state) and report (turn record).
    turn = TurnContext(
        request_key=(
            str(user_message.uuid_id)
            if user_message is not None
            else (f"guest:{x.client_message_id}" if x.client_message_id else None)
        ),
        user_id=user_id,
        guest_session_id=guest_id,
    )
    # Filled while streaming; read by the after-response learning task.
    learned_this_turn: dict[str, bool] = {}

    async def events():
        request_deadline = getattr(
            request.state,
            "deadline",
            asyncio.get_running_loop().time() + settings.agent_deadline_seconds,
        )
        context_tokens = bind_log_context(user_id=user_id or guest_id)
        chat_turn_token = start_chat_turn()
        stream = None
        disconnect_task = None
        ping_task = None
        next_chunk = None
        emitted_answer = False
        guest_answer_chunks = []
        answer_chunks = []
        stream_metadata = {}
        stream_started_at = time.monotonic()
        final_status = "error"
        error_code = "STREAM_FAILED"

        async def persist_assistant(status, *, error=None):
            if not u or assistant_message is None:
                return

            def update_message():
                with SessionLocal() as db:
                    current = db.scalar(select(Message).where(Message.uuid_id == assistant_message.uuid_id))
                    if current is None:
                        return
                    current.status = status
                    current.content = "".join(answer_chunks) if status in {"complete", "stopped"} else ""
                    current.error_code = error
                    current.latency_ms = int((time.monotonic() - stream_started_at) * 1000)
                    current.provider = stream_metadata.get("provider") or "groq"
                    current.model = stream_metadata.get("model") or settings.groq_deep_model
                    current.usage_json = stream_metadata.get("usage") or {}
                    current.sources_json = answer_sources or None
                    if status in {"complete", "stopped"} and turn.state_out is not None:
                        current.turn_state = turn.state_out
                    films = [
                        r for r in answer_references if isinstance(r, dict) and r.get("kind") != "person"
                    ]
                    if status in {"complete", "stopped"} and films:
                        MemoryService(db).save_movie_references(
                            user_id,
                            conversation_id,
                            current.id,
                            [{**r, "source": ANSWER_CARD} for r in films],
                        )
                    conversation = db.get(Conversation, conversation_id)
                    if conversation is not None:
                        conversation.updated_at = utcnow()
                        if status == "complete" and not conversation.title_user_set:
                            if conversation.title == "New movie conversation":
                                conversation.title = _fallback_conversation_title(x.message)
                    db.commit()

            await run_sync(update_message)

        stop_watching_disconnect = asyncio.Event()

        async def wait_for_disconnect():
            # ASGI exposes disconnect state through polling, not an awaitable event.
            # Stop via an Event, not just task.cancel(): Starlette's
            # is_disconnected() runs inside an anyio cancel scope that can
            # swallow an asyncio cancellation delivered at the wrong moment,
            # which used to leave this loop running and the SSE response
            # open forever after a fast error/replay turn.
            while not stop_watching_disconnect.is_set():
                if await request.is_disconnected():
                    return
                try:
                    await asyncio.wait_for(stop_watching_disconnect.wait(), timeout=0.1)
                except TimeoutError:
                    continue

        async def wait_for_ping():
            await asyncio.sleep(15)

        chunk_queue: asyncio.Queue = asyncio.Queue()
        answer_sources: list = []
        # The cards the reply showed, saved with it so a reloaded chat shows them (2026-10-15).
        answer_references: list = []
        pump_task = None

        async def _stop_stream():
            nonlocal pump_task
            if pump_task is not None and not pump_task.done():
                pump_task.cancel()
                await asyncio.gather(pump_task, return_exceptions=True)
            elif pump_task is None and stream is not None:
                await stream.aclose()

        try:
            yield _sse_event(
                "started",
                {
                    "conversation_id": conversation_id,
                    "message_id": assistant_message.uuid_id,
                },
            )
            log_event(
                "stream_started",
                details={"conversation_id": conversation_id},
                user_id=user_id or guest_id,
            )
            if replay or isinstance(guest_replay, dict):
                if assistant_message.content:
                    yield _sse_event("token", {"text": assistant_message.content})
                final_status = "complete"
                yield _sse_event(
                    "done",
                    {"message_id": assistant_message.uuid_id, "usage": {}},
                )
                return
            # Always use the native stream in production. Falling back to
            # run() here would wait for the complete answer and falsely
            # present it as progressive output.
            if guest_id:
                stream = agent.stream(
                    x.message,
                    user_id,
                    conversation_id,
                    guest_history=guest_history,
                    guest_session_id=guest_id,
                    user_timezone=x.timezone,
                    images=x.images,
                    request_key=turn.request_key,
                )
            else:
                stream = agent.stream(
                    ("Continue the previous answer from where it stopped." if x.continue_from else x.message),
                    user_id,
                    conversation_id,
                    persisted_user_message_id=user_message.id,
                    persist_assistant=False,
                    continue_from_message_id=continue_from_message_id,
                    user_timezone=x.timezone,
                    images=x.images,
                    request_key=turn.request_key,
                )
            # Consume the agent stream inside ONE long-lived task. Context
            # variables bound by the agent (tool budgets, stream metadata)
            # must be set and reset in the same context; pulling each chunk
            # in a fresh task breaks that and fails multi-chunk answers.
            # The agent sees the request's deadline (less a moment to send the
            # last events and save the reply) and budgets every stage against it.
            from app.agent.turn import bind_turn
            from app.core.deadline import turn_deadline

            with turn_deadline(request_deadline - 1.0), bind_turn(turn):
                pump_task = asyncio.create_task(_pump_agent_stream(stream, chunk_queue))
            disconnect_task = asyncio.create_task(wait_for_disconnect())
            ping_task = asyncio.create_task(wait_for_ping())
            while True:
                next_chunk = asyncio.create_task(chunk_queue.get())
                done, _ = await asyncio.wait(
                    {next_chunk, disconnect_task, ping_task},
                    return_when=asyncio.FIRST_COMPLETED,
                    timeout=max(
                        0,
                        request_deadline - asyncio.get_running_loop().time(),
                    ),
                )
                if not done:
                    raise RequestDeadlineExceeded()
                if disconnect_task in done:
                    await _stop_stream()
                    final_status = "stopped"
                    log_event(
                        "stream_disconnected",
                        level=logging.INFO,
                        details={"conversation_id": conversation_id},
                        user_id=user_id or guest_id,
                    )
                    return
                if ping_task in done and next_chunk not in done:
                    yield ": ping\n\n"
                    ping_task = asyncio.create_task(wait_for_ping())
                    next_chunk.cancel()
                    await asyncio.gather(next_chunk, return_exceptions=True)
                    next_chunk = None
                    continue
                kind, value = next_chunk.result()
                next_chunk = None
                if kind == "error":
                    raise value
                if kind == "end":
                    stream_metadata = value or {}
                    break
                delta = value
                if delta:
                    if delta.startswith("event: status\n"):
                        data_lines = [line[6:] for line in delta.splitlines() if line.startswith("data: ")]
                        payload = json.loads("\n".join(data_lines))
                        yield _sse_event(
                            "status",
                            {
                                "stage": str(payload.get("stage", "working")),
                                "label": str(payload.get("label") or payload.get("message") or "Working"),
                            },
                        )
                    elif delta.startswith(("event: providers\n", "event: data\n")):
                        data_lines = [line[6:] for line in delta.splitlines() if line.startswith("data: ")]
                        payload = json.loads("\n".join(data_lines))
                        yield _sse_event("providers", {"providers": payload})
                    elif delta.startswith("event: state\n"):
                        data_lines = [line[6:] for line in delta.splitlines() if line.startswith("data: ")]
                        payload = StreamStateEvent.model_validate(json.loads("\n".join(data_lines)))
                        yield _sse_event("state", payload.model_dump(exclude={"event"}))
                    elif delta.startswith("event: references\n"):
                        data_lines = [line[6:] for line in delta.splitlines() if line.startswith("data: ")]
                        payload = json.loads("\n".join(data_lines))
                        shown_references = list(payload.get("references") or [])[:12]
                        answer_references.extend(shown_references)
                        yield _sse_event("references", {"references": shown_references})
                    elif delta.startswith("event: memory\n"):
                        data_lines = [line[6:] for line in delta.splitlines() if line.startswith("data: ")]
                        payload = json.loads("\n".join(data_lines))
                        items = [
                            {
                                "kind": str(i.get("kind")),
                                "id": i.get("id"),
                                "label": str(i.get("label") or "")[:120],
                            }
                            for i in payload.get("items") or []
                            if isinstance(i, dict) and isinstance(i.get("id"), int)
                        ][:8]
                        if items:
                            learned_this_turn["explicit"] = True
                            yield _sse_event("memory", {"items": items})
                    elif delta.startswith("event: action\n"):
                        # A verified list change, shown as a card with its Undo.
                        data_lines = [line[6:] for line in delta.splitlines() if line.startswith("data: ")]
                        payload = json.loads("\n".join(data_lines))
                        text = str(payload.get("text") or "")[:2000]
                        if text:
                            yield _sse_event("action", {"text": text})
                    elif delta.startswith("event: sources\n"):
                        data_lines = [line[6:] for line in delta.splitlines() if line.startswith("data: ")]
                        payload = json.loads("\n".join(data_lines))
                        answer_sources[:] = list(payload.get("sources") or [])[:8]
                        event_payload: dict[str, object] = {"sources": answer_sources}
                        if isinstance(payload.get("assessment"), dict):
                            event_payload["assessment"] = payload["assessment"]
                        yield _sse_event("sources", event_payload)
                    else:
                        if not emitted_answer:
                            record_first_token("chat", stream_started_at)
                            turn.note_first_token()
                        emitted_answer = True
                        answer_chunks.append(delta)
                        if guest_id:
                            guest_answer_chunks.append(delta)
                        yield _sse_event("token", {"text": delta})
            if emitted_answer:
                if guest_id:
                    await _store_guest_turn(
                        guest_id,
                        conversation_id,
                        guest_history,
                        x.message,
                        "".join(guest_answer_chunks),
                        client_message_id=(str(x.client_message_id) if x.client_message_id else None),
                        assistant_message_id=assistant_message.uuid_id,
                        turn_state=turn.state_out,
                    )
                await persist_assistant("complete")
                final_status = "complete"
                yield _sse_event(
                    "done",
                    {
                        "message_id": assistant_message.uuid_id,
                        "provider": stream_metadata.get("provider"),
                        "model": stream_metadata.get("model"),
                        "usage": stream_metadata.get("usage") or {},
                        **_followups(turn.state_out, x.message, signed_in=bool(user_id)),
                    },
                )
                log_event(
                    "stream_completed",
                    details={"conversation_id": conversation_id},
                    user_id=user_id or guest_id,
                )
            else:
                # The stream ended without producing any answer text. That is
                # an incomplete turn, not a "no evidence" finding: telling a
                # user who typed "hi" that no reliable evidence exists for
                # their claim is wrong. Keep it retryable.
                failure = failure_response(AgentFailure(FailureKind.UNKNOWN, "empty answer stream"))
                error_code = failure.code
                yield _sse_event(
                    "error",
                    {
                        "code": failure.code,
                        "message": failure.message,
                        "next_step": failure.next_step,
                        "retryable": failure.retryable,
                        "retry_after_seconds": failure.retry_after_seconds,
                    },
                )
        except asyncio.CancelledError:
            final_status = "stopped"
            await _stop_stream()
            raise
        except GeneratorExit:
            final_status = "stopped"
            await _stop_stream()
            raise
        except ValueError as exc:
            failure = failure_response(exc)
            error_code = failure.code
            yield _sse_event(
                "error",
                {
                    "code": failure.code,
                    "message": failure.message,
                    "next_step": failure.next_step,
                    "retryable": failure.retryable,
                    "retry_after_seconds": failure.retry_after_seconds,
                },
            )
        except RequestDeadlineExceeded:
            error_code = "deadline_exceeded"
            yield _sse_event(
                "error",
                {
                    "code": "deadline_exceeded",
                    "message": "That took longer than expected, so I stopped before giving you an incomplete answer.",
                    "next_step": "Please retry, or narrow the question.",
                    "retryable": True,
                },
            )
        except TimeoutError:
            failure = failure_response(TimeoutError())
            error_code = failure.code
            yield _sse_event(
                "error",
                {
                    "code": failure.code,
                    "message": failure.message,
                    "next_step": failure.next_step,
                    "retryable": failure.retryable,
                    "retry_after_seconds": failure.retry_after_seconds,
                },
            )
        except Exception as exc:
            logger.exception(
                "chat_stream_failed user_id=%s guest_session_id=%s conversation_id=%s",
                user_id,
                guest_id,
                conversation_id,
            )
            failure = failure_response(exc)
            error_code = failure.code
            yield _sse_event(
                "error",
                {
                    "code": failure.code,
                    "message": failure.message,
                    "next_step": failure.next_step,
                    "retryable": failure.retryable,
                    "retry_after_seconds": failure.retry_after_seconds,
                },
            )
        finally:
            if u and not replay and final_status != "complete":
                await persist_assistant(final_status, error=error_code if final_status == "error" else None)
            if next_chunk is not None and not next_chunk.done():
                next_chunk.cancel()
                await asyncio.gather(next_chunk, return_exceptions=True)
            await _stop_stream()
            stop_watching_disconnect.set()
            for helper_task in (disconnect_task, ping_task):
                if helper_task is not None and not helper_task.done():
                    helper_task.cancel()
                    # Bounded: never let a helper's cleanup hold the response open.
                    await asyncio.wait({helper_task}, timeout=1.0)
            _record_turn(turn, "replayed" if replay else final_status, stream_metadata)
            finish_chat_turn(chat_turn_token, final_status)
            if guest_id and x.client_message_id and guest_replay is None:
                # Finished (a completed answer is now cached for replay): a retry may run.
                await shared_state.release(f"guest-answering:{guest_id}:{x.client_message_id}")
            reset_log_context(context_tokens)

    streaming_response = StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        background=background_tasks,
    )
    if u and background_tasks is not None:
        background_tasks.add_task(_compact_conversation_after_response, conversation_id)
        if not replay and x.regenerate_of is None and x.continue_from is None:
            # Learn from a message once: a retry or regeneration of the same message
            # must not count its taste signals twice.
            background_tasks.add_task(
                _ingest_chat_memory_after_response,
                user_id,
                x.message,
                user_message.id if user_message is not None else None,
                learned_this_turn,
            )
        if (
            not replay
            and x.conversation_id is None
            and x.regenerate_of is None
            and x.continue_from is None
            and x.edit_message_id is None
        ):
            background_tasks.add_task(
                _generate_conversation_title,
                conversation_id,
                x.message,
                _fallback_conversation_title(x.message),
            )
    if guest_id:
        _set_guest_cookie(streaming_response, guest_id)
    return streaming_response


def _encode_page_cursor(payload: dict) -> str:
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode_page_cursor(cursor: str | None, expected_scope: str) -> dict | None:
    if not cursor:
        return None
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        value = json.loads(raw)
    except (ValueError, json.JSONDecodeError, UnicodeDecodeError):
        raise HTTPException(422, "Invalid pagination cursor.") from None
    if not isinstance(value, dict) or value.get("scope") != expected_scope:
        raise HTTPException(422, "Invalid pagination cursor.") from None
    return value


def _project_for_new_chat(db, user_id: int, project_id: int | None) -> int | None:
    """The project a new chat starts in: the requested one if it is the user's, else none."""
    if project_id is None:
        return None
    from app.routers.projects import owned_project

    return owned_project(db, user_id, project_id).id


def _followups(state: dict | None, user_message: str, *, signed_in: bool) -> dict:
    """Follow-up suggestions and one-tap options for a reply, from its saved working state."""
    from app.agent.followups import followups

    if not isinstance(state, dict):
        return {"options": [], "suggestions": []}
    try:
        return followups(state, intent=state.get("intent"), user_message=user_message, signed_in=signed_in)
    except (TypeError, ValueError, KeyError, AttributeError) as exc:
        logger.warning("followups_failed error=%r", exc)
        return {"options": [], "suggestions": []}


def _like_pattern(needle: str) -> str:
    """An ILIKE pattern matching the text anywhere, with the user's % and _ taken literally."""
    escaped = needle.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


_SNIPPET_MARKUP = re.compile(r"[*_`#>|]+|\[(\d{1,3})\]")
_SNIPPET_SPACE = re.compile(r"\s+")


def search_snippet(content: str, needle: str, width: int = 140) -> dict | None:
    """A short excerpt of a message around the first match, with the match's offsets in it.

    Markdown markers are removed so the excerpt reads as plain text in the sidebar.
    """
    plain = _SNIPPET_SPACE.sub(" ", _SNIPPET_MARKUP.sub("", content or "")).strip()
    index = plain.casefold().find(needle.casefold())
    if index < 0:
        return None
    before = max(0, (width - len(needle)) // 3)
    start = max(0, index - before)
    if start:
        space = plain.find(" ", start)
        start = space + 1 if 0 <= space < index else start
    end = min(len(plain), start + width)
    if end < len(plain):
        space = plain.rfind(" ", index + len(needle), end)
        end = space if space > 0 else end
    text_ = ("…" if start else "") + plain[start:end] + ("…" if end < len(plain) else "")
    offset = index - start + (1 if start else 0)
    return {"text": text_, "match_start": offset, "match_end": offset + len(needle)}


def _search_snippets(db, rows, needle: str) -> dict[int, dict]:
    """For each listed conversation found by its messages (not its title), the newest matching excerpt."""
    folded = needle.casefold()
    ids = [c.id for c in rows if folded not in (c.title or "").casefold()]
    if not ids:
        return {}
    found: dict[int, dict] = {}
    matches = db.execute(
        select(Message.conversation_id, Message.role, Message.content)
        .where(
            Message.conversation_id.in_(ids),
            Message.is_deleted.is_(False),
            Message.role.in_(("user", "assistant")),
            Message.content.ilike(_like_pattern(needle), escape="\\"),
        )
        .order_by(Message.created_at.desc(), Message.id.desc())
        .limit(len(ids) * 4)
    ).all()
    for conversation_id, role, content in matches:
        if conversation_id in found:
            continue
        snippet = search_snippet(content, needle)
        if snippet:
            found[conversation_id] = {**snippet, "role": role}
    return found


def conversations_v1(
    q: str | None = None,
    cursor: str | None = None,
    limit: int = 30,
    archived: bool = False,
    project: int | None = None,
    u=Depends(user_dep),
):
    return conversations(u, q, cursor, limit, archived, project)


def conversations(
    u=Depends(user_dep),
    q: str | None = None,
    cursor: str | None = None,
    limit: int = 30,
    archived: bool = False,
    project: int | None = None,
):
    if not 1 <= limit <= 100:
        raise HTTPException(422, "Invalid conversation pagination.") from None
    cursor_data = _decode_page_cursor(cursor, "conversations")
    db = SessionLocal()
    try:
        query = select(Conversation).where(
            Conversation.user_id == u.id,
            Conversation.is_deleted.is_(False),
            Conversation.is_archived.is_(archived),
        )
        if project is not None:
            query = query.where(Conversation.project_id == project)
        needle = (q or "").strip()[:120]
        if needle:
            pattern = _like_pattern(needle)
            in_messages = (
                select(Message.id)
                .where(
                    Message.conversation_id == Conversation.id,
                    Message.is_deleted.is_(False),
                    Message.role.in_(("user", "assistant")),
                    Message.content.ilike(pattern, escape="\\"),
                )
                .exists()
            )
            query = query.where(or_(Conversation.title.ilike(pattern, escape="\\"), in_messages))
        if cursor_data:
            try:
                cursor_updated = datetime.fromisoformat(cursor_data["updated_at"])
                cursor_id = int(cursor_data["id"])
                cursor_pinned = bool(cursor_data["pinned"])
            except (KeyError, TypeError, ValueError):
                raise HTTPException(422, "Invalid pagination cursor.") from None
            after_cursor = or_(
                Conversation.updated_at < cursor_updated,
                and_(
                    Conversation.updated_at == cursor_updated,
                    Conversation.id < cursor_id,
                ),
            )
            query = query.where(
                or_(
                    Conversation.is_pinned.is_(False),
                    and_(Conversation.is_pinned.is_(True), after_cursor),
                )
                if cursor_pinned
                else and_(Conversation.is_pinned.is_(False), after_cursor)
            )
        rows = db.scalars(
            query.order_by(
                Conversation.is_pinned.desc(),
                Conversation.updated_at.desc(),
                Conversation.id.desc(),
            ).limit(limit + 1)
        ).all()
        has_more = len(rows) > limit
        rows = rows[:limit]
        snippets = _search_snippets(db, rows, needle) if needle else {}
        items = [
            {
                "id": c.id,
                "title": c.title,
                "created_at": c.created_at,
                "updated_at": c.updated_at,
                "is_pinned": c.is_pinned,
                "is_archived": c.is_archived,
                "project_id": c.project_id,
                **({"snippet": snippets[c.id]} if c.id in snippets else {}),
            }
            for c in rows
        ]
        next_cursor = None
        if has_more and rows:
            last = rows[-1]
            next_cursor = _encode_page_cursor(
                {
                    "scope": "conversations",
                    "pinned": bool(last.is_pinned),
                    "updated_at": last.updated_at.isoformat(),
                    "id": last.id,
                }
            )
        return {"items": items, "next_cursor": next_cursor}
    finally:
        db.close()


def conversation_v1(cid: int, u=Depends(user_dep)):
    return conversation(cid, u)


def conversation_messages_v1(
    cid: int,
    cursor: str | None = None,
    limit: int = 30,
    u=Depends(user_dep),
):
    if not 1 <= limit <= 100:
        raise HTTPException(422, "Invalid message pagination.") from None
    scope = f"messages:{cid}"
    cursor_data = _decode_page_cursor(cursor, scope)
    db = SessionLocal()
    try:
        conversation = db.scalar(
            select(Conversation).where(
                Conversation.id == cid,
                Conversation.user_id == u.id,
                Conversation.is_deleted.is_(False),
            )
        )
        if conversation is None:
            raise HTTPException(404, "Conversation not found.") from None
        query = select(Message).where(
            Message.conversation_id == cid,
            Message.is_deleted.is_(False),
        )
        if cursor_data:
            try:
                created_at = datetime.fromisoformat(cursor_data["created_at"])
                message_id = int(cursor_data["id"])
            except (KeyError, TypeError, ValueError):
                raise HTTPException(422, "Invalid pagination cursor.") from None
            query = query.where(
                or_(
                    Message.created_at < created_at,
                    and_(
                        Message.created_at == created_at,
                        Message.id < message_id,
                    ),
                )
            )
        rows = db.scalars(query.order_by(Message.created_at.desc(), Message.id.desc()).limit(limit + 1)).all()
        has_more = len(rows) > limit
        page_rows = rows[:limit]
        next_cursor = None
        if has_more and page_rows:
            last = page_rows[-1]
            next_cursor = _encode_page_cursor(
                {
                    "scope": scope,
                    "created_at": last.created_at.isoformat(),
                    "id": last.id,
                }
            )
        page_rows.reverse()
        references: dict[int, list[dict[str, object]]] = {}
        assistant_ids = [message.id for message in page_rows if message.role == "assistant"]
        if assistant_ids:
            for ref in db.scalars(
                select(MovieReference)
                .where(MovieReference.message_id.in_(assistant_ids))
                .order_by(MovieReference.ordinal)
            ):
                references.setdefault(ref.message_id, []).append({"tmdb_id": ref.tmdb_id, "title": ref.title})
        return {
            "items": [
                {
                    "id": message.uuid_id,
                    "parent_id": message.parent_id,
                    "role": message.role,
                    "content": message.content,
                    "status": message.status,
                    "model": message.model,
                    "sources": message.sources_json or [],
                    "references": references.get(message.id, [])[:12],
                    "latency_ms": message.latency_ms,
                    "feedback": message.feedback,
                    "feedback_reason": message.feedback_reason,
                    **(
                        _followups(message.turn_state, "", signed_in=True)
                        if message.role == "assistant" and message.status == "complete"
                        else {}
                    ),
                    "error_code": message.error_code,
                    "created_at": message.created_at,
                }
                for message in page_rows
            ],
            "next_cursor": next_cursor,
        }
    finally:
        db.close()


def conversation(cid: int, u=Depends(user_dep)):
    db = SessionLocal()
    try:
        c = db.scalar(
            select(Conversation).where(
                Conversation.id == cid,
                Conversation.user_id == u.id,
                Conversation.is_deleted.is_(False),
            )
        )
        if not c:
            raise HTTPException(404, "Conversation not found") from None
        active_movie = db.scalar(
            select(MovieReference.title)
            .where(
                MovieReference.conversation_id == cid,
                MovieReference.user_id == u.id,
                MovieReference.source != ANSWER_CARD,
            )
            .order_by(
                MovieReference.created_at.desc(),
                MovieReference.ordinal.asc(),
                MovieReference.id.asc(),
            )
            .limit(1)
        )
        return {
            "id": cid,
            "title": c.title,
            "active_movie_title": active_movie,
            "created_at": c.created_at,
            "updated_at": c.updated_at,
            "is_pinned": c.is_pinned,
            "is_archived": c.is_archived,
            "project_id": c.project_id,
        }
    finally:
        db.close()


def rename_conversation_v1(cid: int, payload: ConversationRenameIn, u=Depends(user_dep)):
    return rename_conversation(cid, payload, u)


def rename_conversation(cid: int, payload: ConversationRenameIn, u=Depends(user_dep)):
    db = SessionLocal()
    try:
        owned_conversation = db.scalar(
            select(Conversation).where(
                Conversation.id == cid,
                Conversation.user_id == u.id,
                Conversation.is_deleted.is_(False),
            )
        )
        if owned_conversation is None:
            raise HTTPException(404, "Conversation not found") from None
        owned_conversation.title = payload.title.strip()
        if not owned_conversation.title:
            raise HTTPException(422, "Conversation title cannot be empty") from None
        owned_conversation.title_user_set = True
        db.commit()
        return {"id": cid, "title": owned_conversation.title}
    finally:
        db.close()


def delete_conversation_v1(cid: int, u=Depends(user_dep)):
    return delete_conversation(cid, u)


def delete_conversation(cid: int, u=Depends(user_dep)):
    db = SessionLocal()
    try:
        owned_conversation = db.scalar(
            select(Conversation).where(
                Conversation.id == cid,
                Conversation.user_id == u.id,
                Conversation.is_deleted.is_(False),
            )
        )
        if owned_conversation is None:
            raise HTTPException(404, "Conversation not found") from None
        deleted_at = utcnow()
        db.execute(
            update(Message)
            .where(Message.conversation_id == cid, Message.is_deleted.is_(False))
            .values(is_deleted=True, deleted_at=deleted_at)
        )
        owned_conversation.is_deleted = True
        owned_conversation.deleted_at = deleted_at
        db.commit()
        return {
            "ok": True,
            "id": cid,
            "undo_until": (deleted_at + timedelta(days=30)).isoformat(),
        }
    finally:
        db.close()


def restore_conversation(cid: int, u=Depends(user_dep)):
    with SessionLocal() as db:
        conversation = db.scalar(
            select(Conversation).where(
                Conversation.id == cid,
                Conversation.user_id == u.id,
                Conversation.is_deleted.is_(True),
                Conversation.deleted_at >= utcnow() - timedelta(days=30),
            )
        )
        if conversation is None:
            raise HTTPException(404, "Conversation cannot be restored.") from None
        conversation.is_deleted = False
        conversation.deleted_at = None
        db.execute(
            update(Message).where(Message.conversation_id == cid).values(is_deleted=False, deleted_at=None)
        )
        db.commit()
        return {"ok": True, "id": cid}


def pin_conversation(cid: int, pinned: bool, u=Depends(user_dep)):
    with SessionLocal() as db:
        conversation = db.scalar(
            select(Conversation).where(
                Conversation.id == cid,
                Conversation.user_id == u.id,
                Conversation.is_deleted.is_(False),
            )
        )
        if conversation is None:
            raise HTTPException(404, "Conversation not found.") from None
        conversation.is_pinned = pinned
        db.commit()
        return {"id": cid, "is_pinned": pinned}


def archive_conversation(cid: int, archived: bool, u=Depends(user_dep)):
    with SessionLocal() as db:
        conversation = db.scalar(
            select(Conversation).where(
                Conversation.id == cid,
                Conversation.user_id == u.id,
                Conversation.is_deleted.is_(False),
            )
        )
        if conversation is None:
            raise HTTPException(404, "Conversation not found.") from None
        conversation.is_archived = archived
        db.commit()
        return {"id": cid, "is_archived": archived}


FEEDBACK_REASONS = ("wrong_film", "wrong_facts", "ignored_request", "too_long", "not_useful", "other")


class MessageFeedbackDetail(BaseModel):
    """Why an answer was not helpful. Sent with a thumbs down; kept for review and eval cases."""

    reason: (
        Literal["wrong_film", "wrong_facts", "ignored_request", "too_long", "not_useful", "other"] | None
    ) = None
    note: str | None = Field(default=None, max_length=1000)


def set_message_feedback(
    message_id: uuid.UUID,
    feedback: Literal["up", "down", "none"],
    detail: MessageFeedbackDetail | None = None,
    u=Depends(user_dep),
):
    """Mark an answer helpful, not helpful (optionally with a reason), or clear the mark ("none")."""
    with SessionLocal() as db:
        message = db.scalar(
            select(Message).where(
                Message.uuid_id == str(message_id),
                Message.user_id == u.id,
                Message.role == "assistant",
            )
        )
        if message is None:
            raise HTTPException(404, "Message not found.") from None
        if feedback == "none":
            message.feedback, message.feedback_reason, message.feedback_note = None, None, None
        else:
            if feedback != message.feedback:
                message.feedback_reason, message.feedback_note = None, None
            message.feedback = feedback
            if feedback == "down" and detail is not None:
                message.feedback_reason = detail.reason
                note = (detail.note or "").strip()
                message.feedback_note = note or None
        db.commit()
        return {
            "id": message.uuid_id,
            "feedback": message.feedback,
            "feedback_reason": message.feedback_reason,
        }


def state_v1(u=Depends(user_dep)):
    return state(u)


def state(u=Depends(user_dep), limit: int = Query(100, ge=1, le=100)):
    # FastAPI injects an int at runtime; direct internal/test calls see the Query object.
    # Normalize both forms at this boundary so bounded collection queries stay valid.
    limit = max(1, min(100, int(getattr(limit, "default", limit))))
    db = SessionLocal()
    try:

        def refs(items):
            ids = [x.movie_id for x in items]
            movies = (
                {m.id: m for m in db.scalars(select(Movie).where(Movie.id.in_(ids))).all()} if ids else {}
            )

            def to_ref(item):
                movie = movies.get(item.movie_id)
                metadata = movie.metadata_json if movie and isinstance(movie.metadata_json, dict) else {}
                release_date = metadata.get("release_date")
                return {
                    "movie_id": item.movie_id,
                    "tmdb_id": movie.tmdb_id if movie else None,
                    "title": movie.title if movie else None,
                    "poster_path": metadata.get("poster_path"),
                    "poster_url": metadata.get("poster_url"),
                    "release_date": release_date,
                    "year": str(release_date)[:4] if release_date else None,
                    "vote_average": metadata.get("vote_average"),
                    "overview": metadata.get("overview"),
                    "rating": user_ratings.get(item.movie_id),
                    "created_at": getattr(item, "created_at", None),
                    "watched_at": getattr(item, "watched_at", None),
                }

            return [to_ref(item) for item in items]

        wl_total = (
            db.scalar(select(func.count()).select_from(WatchlistItem).where(WatchlistItem.user_id == u.id))
            or 0
        )
        wd_total = (
            db.scalar(select(func.count()).select_from(WatchedItem).where(WatchedItem.user_id == u.id)) or 0
        )
        wl = db.scalars(
            select(WatchlistItem)
            .where(WatchlistItem.user_id == u.id)
            .order_by(WatchlistItem.created_at.desc(), WatchlistItem.id.desc())
            .limit(limit)
        ).all()
        wd = db.scalars(
            select(WatchedItem)
            .where(WatchedItem.user_id == u.id)
            .order_by(WatchedItem.watched_at.desc(), WatchedItem.id.desc())
            .limit(limit)
        ).all()
        rating_total = db.scalar(select(func.count()).select_from(Rating).where(Rating.user_id == u.id)) or 0
        rt = db.scalars(
            select(Rating)
            .where(Rating.user_id == u.id)
            .order_by(Rating.updated_at.desc(), Rating.id.desc())
            .limit(limit)
        ).all()
        user_ratings = {rating.movie_id: rating.rating for rating in rt}
        rating_movie_ids = [rating.movie_id for rating in rt]
        rated_movies = (
            {
                movie.id: movie.tmdb_id
                for movie in db.scalars(select(Movie).where(Movie.id.in_(rating_movie_ids))).all()
            }
            if rating_movie_ids
            else {}
        )
        preference_total = (
            db.scalar(select(func.count()).select_from(Preference).where(Preference.user_id == u.id)) or 0
        )
        prefs = db.scalars(
            select(Preference)
            .where(Preference.user_id == u.id)
            .order_by(Preference.strength.desc(), Preference.id.desc())
            .limit(limit)
        ).all()
        memories = db.scalars(
            select(Memory)
            .where(Memory.user_id == u.id)
            .order_by(Memory.importance.desc(), Memory.created_at.desc())
            .limit(50)
        ).all()
        ui_setting = db.scalar(select(UserInterfaceSetting).where(UserInterfaceSetting.user_id == u.id))
        return {
            "region": (
                (ui_setting.region or settings.default_region).upper()
                if ui_setting
                else settings.default_region.upper()
            ),
            "watchlist": refs(wl),
            "watched": refs(wd),
            "watchlist_count": int(wl_total),
            "watched_count": int(wd_total),
            "watchlist_has_more": wl_total > len(wl),
            "watched_has_more": wd_total > len(wd),
            "rating_count": int(rating_total),
            "rating_has_more": rating_total > len(rt),
            "preference_count": int(preference_total),
            "preference_has_more": preference_total > len(prefs),
            "ratings": [
                {
                    "movie_id": rating.movie_id,
                    "tmdb_id": rated_movies.get(rating.movie_id),
                    "rating": rating.rating,
                }
                for rating in rt
            ],
            "preferences": [
                {
                    "id": x.id,
                    "dimension": x.dimension,
                    "value": x.value,
                    "polarity": x.polarity,
                    "strength": x.strength,
                    "confidence": x.confidence,
                    "evidence_count": x.evidence_count,
                    "source": "explicit" if x.source == "explicit" else "inferred",
                }
                for x in prefs
            ],
            "memories": [
                {
                    "id": memory.id,
                    "type": memory.memory_type,
                    "content": memory.content,
                    "importance": memory.importance,
                    # Provenance for the My Taste page: stated by the user vs noticed by Grace.
                    "source": (
                        "explicit"
                        if memory.memory_type in {"preference", "user_stated"}
                        or str(memory.content or "").startswith(("User said:", "After watching"))
                        else "inferred"
                    ),
                    "created_at": memory.created_at,
                }
                for memory in memories
            ],
            "observed_behavior": [
                f'{len(wd)} film{"s" if len(wd) != 1 else ""} marked watched',
                f'{len(wl)} film{"s" if len(wl) != 1 else ""} saved for later',
                f'{len(rt)} rating{"s" if len(rt) != 1 else ""} recorded',
            ],
            "memory_enabled": (
                personalization_setting.memory_enabled
                if (
                    personalization_setting := db.scalar(
                        select(UserPersonalizationSetting).where(UserPersonalizationSetting.user_id == u.id)
                    )
                )
                else True
            ),
        }
    finally:
        db.close()


def _collection_page(u_id: int, collection: str, limit: int, offset: int):
    model = WatchlistItem if collection == "watchlist" else WatchedItem
    order_column = model.created_at if collection == "watchlist" else model.watched_at
    with SessionLocal() as db:
        total = db.scalar(select(func.count()).select_from(model).where(model.user_id == u_id)) or 0
        items = db.scalars(
            select(model)
            .where(model.user_id == u_id)
            .order_by(order_column.desc(), model.id.desc())
            .offset(offset)
            .limit(limit)
        ).all()
        ids = [item.movie_id for item in items]
        movies = {m.id: m for m in db.scalars(select(Movie).where(Movie.id.in_(ids))).all()} if ids else {}
        return {
            "items": [
                {
                    "movie_id": item.movie_id,
                    "tmdb_id": (movies.get(item.movie_id).tmdb_id if movies.get(item.movie_id) else None),
                    "title": (movies.get(item.movie_id).title if movies.get(item.movie_id) else None),
                    "created_at": getattr(item, "created_at", None),
                    "watched_at": getattr(item, "watched_at", None),
                }
                for item in items
            ],
            "total": int(total),
            "limit": limit,
            "offset": offset,
            "has_more": offset + len(items) < total,
        }


def state_watchlist(
    limit: int = Query(100, ge=1, le=100),
    offset: int = Query(0, ge=0),
    u=Depends(user_dep),
):
    return _collection_page(u.id, "watchlist", limit, offset)


def state_watched(
    limit: int = Query(100, ge=1, le=100),
    offset: int = Query(0, ge=0),
    u=Depends(user_dep),
):
    return _collection_page(u.id, "watched", limit, offset)


async def movie_search_v1(
    request: Request,
    q: str = Query(min_length=1, max_length=200),
    u=Depends(optional_user_dep),
):
    await _enforce_expensive_rate_limit(request, u, "search")
    return await movie_search(q, u)


async def movie_search(q: str = Query(min_length=1, max_length=200), u=Depends(optional_user_dep)):
    t = _movie_tools(db_factory=SessionLocal)
    try:
        return await t.search_movies(q)
    except UpstreamError as exc:
        logger.warning("movie_search_upstream_unavailable query=%s error=%s", q, exc)
        return {"query": q, "page": 1, "total_results": 0, "results": []}
    except RuntimeError as exc:
        raise HTTPException(502, str(exc)) from exc
    except Exception:
        logger.exception("movie_search_failed query=%s", q)
        raise HTTPException(502, "Movie search is temporarily unavailable.") from None


async def compare_visuals_v1(
    request: Request,
    movie_a: int = Query(ge=1),
    movie_b: int = Query(ge=1),
    focus: str = Query("visual atmosphere,lighting,color,composition", max_length=80),
    u=Depends(optional_user_dep),
):
    await _enforce_expensive_rate_limit(request, u, "model")
    return await compare_visuals(movie_a, movie_b, focus, u)


async def movie_details_v1(tmdb_id: int = Path(ge=1), u=Depends(optional_user_dep)):
    return await movie_details(tmdb_id, u)


async def movie_intelligence_v1(
    request: Request,
    tmdb_id: int = Path(ge=1),
    focus: str = Query("broad", max_length=80),
    u=Depends(optional_user_dep),
):
    await _enforce_expensive_rate_limit(request, u)
    return await movie_intelligence(tmdb_id, focus, u)


async def movie_visuals_v1(
    request: Request,
    tmdb_id: int = Path(ge=1),
    focus: str = Query("visual atmosphere,lighting,color,composition", max_length=80),
    u=Depends(optional_user_dep),
):
    await _enforce_expensive_rate_limit(request, u)
    return await movie_visuals(tmdb_id, focus, u)


async def movie_reviews_v1(
    request: Request,
    tmdb_id: int = Path(ge=1),
    focus: str = Query("themes,emotion,craft,story,acting", max_length=80),
    u=Depends(optional_user_dep),
):
    await _enforce_expensive_rate_limit(request, u)
    return await movie_reviews(tmdb_id, focus, u)


async def movie_details(tmdb_id: int, u=Depends(optional_user_dep)):
    t = _movie_tools(db_factory=SessionLocal)
    try:
        payload = await t.get_movie_details(tmdb_id)
        if u is not None:
            region = await run_sync(
                lambda: _with_session(
                    lambda db: db.scalar(
                        select(UserInterfaceSetting.region).where(UserInterfaceSetting.user_id == u.id)
                    )
                )
            )
            region = (region or settings.default_region).upper()
            payload = {
                **payload,
                "provider_region": region,
                "watch_providers_for_region": await t.get_watch_providers(tmdb_id, region),
            }
        return payload
    except UpstreamError as exc:
        raise _movie_upstream_http_error(exc) from exc
    except RuntimeError as exc:
        raise _movie_upstream_http_error(exc) from exc
    except Exception:
        logger.exception("movie_details_failed tmdb_id=%s", tmdb_id)
        raise HTTPException(502, "Movie details are temporarily unavailable.") from None


async def movie_intelligence(tmdb_id: int, focus: str = "broad", u=Depends(optional_user_dep)):
    t = _movie_tools(db_factory=SessionLocal)
    try:
        return await t.movie_intelligence(tmdb_id, focus)
    except UpstreamError as exc:
        raise _movie_upstream_http_error(exc) from exc
    except TimeoutError as exc:
        raise _movie_upstream_http_error(exc) from exc
    except RuntimeError as exc:
        raise _movie_upstream_http_error(exc) from exc
    except Exception:
        logger.exception("movie_intelligence_failed tmdb_id=%s focus=%s", tmdb_id, focus)
        raise HTTPException(502, "Movie intelligence is temporarily unavailable.") from None


class ReminderIn(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    when: str | None = Field(default=None, max_length=120)
    due_at: datetime | None = None
    timezone: str | None = Field(default=None, max_length=64)
    movie_tmdb_id: int | None = Field(default=None, ge=1)
    body: str | None = Field(default=None, max_length=500)


def notifications_v1(u=Depends(user_dep)):
    from app.services.reminders import ReminderService, serialize

    with SessionLocal() as db:
        service = ReminderService(db)
        return {
            "items": [serialize(item) for item in service.visible(u.id)],
            "upcoming": [serialize(item) for item in service.upcoming(u.id)],
            "unread_count": service.unread_count(u.id),
            "reminders_enabled": service.reminders_enabled(u.id),
        }


def notification_read_v1(notification_id: int = Path(ge=1), u=Depends(user_dep)):
    from app.services.reminders import ReminderService

    with SessionLocal() as db:
        if not ReminderService(db).mark_read(u.id, notification_id):
            raise HTTPException(404, "Notification not found or already read.") from None
    return {"status": "read"}


def notifications_read_all_v1(u=Depends(user_dep)):
    from app.services.reminders import ReminderService

    with SessionLocal() as db:
        return {"status": "read", "count": ReminderService(db).mark_read(u.id)}


def notification_dismiss_v1(notification_id: int = Path(ge=1), u=Depends(user_dep)):
    from app.services.reminders import ReminderService

    with SessionLocal() as db:
        if not ReminderService(db).dismiss(u.id, notification_id):
            raise HTTPException(404, "Notification not found.") from None
    return {"status": "dismissed"}


def create_reminder_v1(x: ReminderIn, u=Depends(user_dep)):
    from app.services.reminders import ReminderService, parse_when, user_zone

    due_at = x.due_at or parse_when(x.when, now=datetime.now(UTC), zone=user_zone(x.timezone))
    with SessionLocal() as db:
        result = ReminderService(db).schedule(
            u.id, title=x.title, due_at=due_at, body=x.body, movie_tmdb_id=x.movie_tmdb_id
        )
    if result.status == "disabled":
        raise HTTPException(409, "Reminders are turned off in your settings.") from None
    if result.status in {"unparsed", "past"}:
        raise HTTPException(422, "Choose a reminder time in the future, such as 'tomorrow at 8pm'.") from None
    return {"id": result.notification_id, "due_at": result.due_at.isoformat() if result.due_at else None}


async def person_details_v1(request: Request, person_id: int = Path(ge=1), u=Depends(optional_user_dep)):
    await _enforce_expensive_rate_limit(request, u)
    from app.services.person_intelligence import build_person

    tmdb, _web = agent._shared_services()
    try:
        return await asyncio.wait_for(
            build_person(person_id, tmdb=tmdb), timeout=settings.agent_deadline_seconds
        )
    except (UpstreamError, TimeoutError, RuntimeError) as exc:
        raise _movie_upstream_http_error(exc) from exc
    except (httpx.HTTPError, KeyError, ValueError, TypeError):
        logger.exception("person_details_failed person_id=%s", person_id)
        raise HTTPException(502, "Person details are temporarily unavailable.") from None


async def people_search_v1(
    request: Request, q: str = Query(min_length=1, max_length=200), u=Depends(optional_user_dep)
):
    await _enforce_expensive_rate_limit(request, u, "search")
    from app.services.person_intelligence import search_people

    tmdb, _web = agent._shared_services()
    try:
        return {"query": q, "results": await search_people(q, tmdb=tmdb)}
    except UpstreamError as exc:
        logger.warning("people_search_upstream_unavailable query=%s error=%s", q, exc)
        return {"query": q, "results": []}
    except (httpx.HTTPError, KeyError, ValueError, TypeError):
        logger.exception("people_search_failed query=%s", q)
        raise HTTPException(502, "People search is temporarily unavailable.") from None


async def movie_intelligence_category_v1(
    request: Request,
    tmdb_id: int = Path(ge=1),
    category: Literal["facts", "awards", "music", "locations"] = Path(),
    u=Depends(optional_user_dep),
):
    await _enforce_expensive_rate_limit(request, u)
    return await movie_intelligence_category(tmdb_id, category)


async def movie_intelligence_category(tmdb_id: int, category: str):
    from app.services.movie_intelligence_page import build_category

    t = _movie_tools(db_factory=SessionLocal)
    try:
        details = await t.get_movie_details(tmdb_id)
        return await asyncio.wait_for(
            build_category(category, details), timeout=settings.agent_deadline_seconds
        )
    except (UpstreamError, TimeoutError, RuntimeError) as exc:
        raise _movie_upstream_http_error(exc) from exc
    except Exception:
        logger.exception(
            "movie_intelligence_category_failed tmdb_id=%s category=%s",
            tmdb_id,
            category,
        )
        raise HTTPException(502, "Movie intelligence is temporarily unavailable.") from None


async def movie_visuals(
    tmdb_id: int = Path(ge=1),
    focus: str = "visual atmosphere,lighting,color,composition",
    u=Depends(optional_user_dep),
):
    t = _movie_tools(db_factory=SessionLocal)
    try:
        return await t.visual_intelligence(tmdb_id)
    except UpstreamError as exc:
        raise _movie_upstream_http_error(exc) from exc
    except TimeoutError as exc:
        raise _movie_upstream_http_error(exc) from exc
    except RuntimeError as exc:
        raise _movie_upstream_http_error(exc) from exc
    except Exception:
        logger.exception("movie_visuals_failed tmdb_id=%s", tmdb_id)
        raise HTTPException(502, "Visual analysis unavailable.") from None


async def movie_recommendations_v1(
    request: Request,
    tmdb_id: int = Path(ge=1),
    page: int = Query(1, ge=1, le=500),
    u=Depends(optional_user_dep),
):
    await _enforce_expensive_rate_limit(request, u)
    return await movie_recommendations(tmdb_id, page, u)


async def movie_similar_v1(
    request: Request,
    tmdb_id: int = Path(ge=1),
    page: int = Query(1, ge=1, le=500),
    u=Depends(optional_user_dep),
):
    await _enforce_expensive_rate_limit(request, u)
    return await movie_similar(tmdb_id, page, u)


async def movie_recommendations(
    tmdb_id: int,
    page: int = Query(1, ge=1),
    u=Depends(optional_user_dep),
):
    t = _movie_tools(db_factory=SessionLocal)
    try:
        payload = await t.movie_recommendations(tmdb_id, page)
        if u is None:
            return payload
        profile = await _load_user_profile_async(u.id)
        return t.personalize_movie_results(payload, profile)
    except UpstreamError as exc:
        raise _movie_upstream_http_error(exc) from exc
    except TimeoutError as exc:
        raise _movie_upstream_http_error(exc) from exc
    except RuntimeError as exc:
        raise _movie_upstream_http_error(exc) from exc
    except Exception:
        logger.exception("movie_recommendations_failed tmdb_id=%s page=%s", tmdb_id, page)
        raise HTTPException(502, "Movie recommendations are temporarily unavailable.") from None


async def movie_similar(
    tmdb_id: int,
    page: int = Query(1, ge=1),
    u=Depends(optional_user_dep),
):
    t = _movie_tools(db_factory=SessionLocal)
    try:
        payload = await t.similar_movies(tmdb_id, page)
        if u is None:
            return payload
        profile = await _load_user_profile_async(u.id)
        return t.personalize_movie_results(payload, profile)
    except UpstreamError as exc:
        raise _movie_upstream_http_error(exc) from exc
    except TimeoutError as exc:
        raise _movie_upstream_http_error(exc) from exc
    except RuntimeError as exc:
        raise _movie_upstream_http_error(exc) from exc
    except Exception:
        logger.exception("movie_similar_failed tmdb_id=%s page=%s", tmdb_id, page)
        raise HTTPException(502, "Similar movies are temporarily unavailable.") from None


async def compare_visuals(
    movie_a: int = Query(ge=1),
    movie_b: int = Query(ge=1),
    focus: str = "visual atmosphere,lighting,color,composition",
    u=Depends(optional_user_dep),
):
    t = _movie_tools(db_factory=SessionLocal)
    try:
        return await asyncio.wait_for(
            agent.compare_visuals(t, movie_a, movie_b, focus),
            timeout=settings.tool_timeout_seconds,
        )
    except (UpstreamError, TimeoutError) as exc:
        raise _movie_upstream_http_error(exc) from exc
    except Exception:
        logger.exception("compare_visuals_failed movie_a=%s movie_b=%s", movie_a, movie_b)
        raise HTTPException(502, "Visual comparison is temporarily unavailable.") from None


async def movie_reviews(
    tmdb_id: int = Path(ge=1),
    focus: str = "themes,emotion,craft,story,acting",
    u=Depends(optional_user_dep),
):
    t = _movie_tools(db_factory=SessionLocal)
    try:
        return await t.review_intelligence(tmdb_id, focus)
    except UpstreamError as exc:
        raise _movie_upstream_http_error(exc) from exc
    except TimeoutError as exc:
        raise _movie_upstream_http_error(exc) from exc
    except RuntimeError as exc:
        raise _movie_upstream_http_error(exc) from exc
    except Exception:
        logger.exception("movie_reviews_failed tmdb_id=%s", tmdb_id)
        raise HTTPException(502, "Review analysis is temporarily unavailable.") from None


async def add_movie_watchlist_v1(tmdb_id: int = Path(ge=1), u=Depends(user_dep)):
    return await add_movie_watchlist(tmdb_id, u)


async def add_movie_watchlist(tmdb_id: int, u=Depends(user_dep)):
    t = _movie_tools(db_factory=SessionLocal)
    try:
        region = await run_sync(
            lambda: _with_session(
                lambda db: db.scalar(
                    select(UserInterfaceSetting.region).where(UserInterfaceSetting.user_id == u.id)
                )
            )
        )
        return await MovieStateService(None, t.tmdb, db_factory=SessionLocal).add_watchlist(
            u.id, tmdb_id, region=region
        )
    except RuntimeError as exc:
        raise HTTPException(502, str(exc)) from exc
    except Exception:
        logger.exception("add_watchlist_failed tmdb_id=%s user_id=%s", tmdb_id, u.id)
        raise HTTPException(502, "Could not update the watchlist.") from None


async def undo_guest_watchlist_removal(
    request: Request,
    response: Response,
    tmdb_id: int = Path(ge=1),
):
    client_ip = request.client.host if request.client else "unknown"
    if not await shared_state.rate_limit(f"guest-undo-ip:{client_ip}", 10, 60):
        raise HTTPException(
            429,
            "Too many undo requests. Please wait a moment.",
            headers={"Retry-After": "60"},
        ) from None
    guest_id = await _guest_session_id(request, response)
    undo_key = f"guest-watchlist-undo:{guest_id}:{tmdb_id}"
    item = await shared_state.cache_get(undo_key, 30)
    if not isinstance(item, dict) or int(item.get("tmdb_id", -1)) != tmdb_id:
        raise HTTPException(404, "That watchlist removal can no longer be undone.") from None

    watchlist_key = f"guest-watchlist:{guest_id}"
    saved = await shared_state.cache_get(watchlist_key, _GUEST_SESSION_TTL_SECONDS)
    saved = saved if isinstance(saved, list) else []
    if not any(int(saved_item.get("tmdb_id", -1)) == tmdb_id for saved_item in saved):
        saved.append(item)
        await shared_state.cache_set(
            watchlist_key,
            saved,
            settings.cache_max_entries,
            ttl=_GUEST_SESSION_TTL_SECONDS,
        )
    await shared_state.cache_set(undo_key, None, settings.cache_max_entries, ttl=30)
    return {"ok": True, "tmdb_id": tmdb_id, "movie": item.get("movie_title")}


async def mark_movie_watched_v1(tmdb_id: int = Path(ge=1), u=Depends(user_dep)):
    return await mark_movie_watched(tmdb_id, u)


async def mark_movie_watched(tmdb_id: int, u=Depends(user_dep)):
    t = _movie_tools()
    try:
        return await MovieStateService(None, t.tmdb, db_factory=SessionLocal).mark_watched(u.id, tmdb_id)
    except RuntimeError as exc:
        raise HTTPException(502, str(exc)) from exc
    except Exception:
        logger.exception("mark_watched_failed tmdb_id=%s user_id=%s", tmdb_id, u.id)
        raise HTTPException(502, "Could not update watched history.") from None


async def move_movie_to_watched_v1(tmdb_id: int = Path(ge=1), u=Depends(user_dep)):
    return await move_movie_to_watched(tmdb_id, u)


async def move_movie_to_watched(tmdb_id: int, u=Depends(user_dep)):
    t = _movie_tools(db_factory=SessionLocal)
    try:
        result = await MovieStateService(None, t.tmdb, db_factory=SessionLocal).move_to_watched(u.id, tmdb_id)
        return result
    except RuntimeError as exc:
        raise HTTPException(502, str(exc)) from exc
    except Exception:
        logger.exception("move_to_watched_failed tmdb_id=%s user_id=%s", tmdb_id, u.id)
        raise HTTPException(502, "Could not move the movie to watched history.") from None


async def remove_movie_watchlist_v1(tmdb_id: int = Path(ge=1), u=Depends(user_dep)):
    return await remove_movie_watchlist(tmdb_id, u)


async def remove_movie_watchlist(tmdb_id: int, u=Depends(user_dep)):
    t = _movie_tools()
    try:
        return await MovieStateService(None, t.tmdb, db_factory=SessionLocal).remove_watchlist(u.id, tmdb_id)
    except Exception:
        logger.exception("remove_watchlist_failed tmdb_id=%s user_id=%s", tmdb_id, u.id)
        raise HTTPException(502, "Could not update the watchlist.") from None


async def unmark_movie_watched_v1(tmdb_id: int = Path(ge=1), u=Depends(user_dep)):
    return await unmark_movie_watched(tmdb_id, u)


async def unmark_movie_watched(tmdb_id: int, u=Depends(user_dep)):
    t = _movie_tools()
    try:
        return await MovieStateService(None, t.tmdb, db_factory=SessionLocal).unmark_watched(u.id, tmdb_id)
    except Exception:
        logger.exception("unmark_watched_failed tmdb_id=%s user_id=%s", tmdb_id, u.id)
        raise HTTPException(502, "Could not update watched history.") from None


async def rate_movie_v1(tmdb_id: int = Path(ge=1), x: RatingIn = ..., u=Depends(user_dep)):
    return await rate_movie(tmdb_id, x, u)


async def rate_movie(tmdb_id: int, x: RatingIn, u=Depends(user_dep)):
    t = _movie_tools()
    try:
        return await MovieStateService(None, t.tmdb, db_factory=SessionLocal).rate(u.id, tmdb_id, x.rating)
    except RuntimeError as exc:
        raise HTTPException(502, str(exc)) from exc
    except Exception:
        logger.exception("rate_movie_failed tmdb_id=%s user_id=%s", tmdb_id, u.id)
        raise HTTPException(502, "Could not save the rating.") from None


async def movie_feedback_v1(tmdb_id: int = Path(ge=1), x: FeedbackIn = ..., u=Depends(user_dep)):
    return await movie_feedback(tmdb_id, x, u)


async def movie_feedback(tmdb_id: int, x: FeedbackIn, u=Depends(user_dep)):
    t = _movie_tools()
    try:
        return await MovieStateService(None, t.tmdb, db_factory=SessionLocal).feedback(
            u.id, tmdb_id, x.feedback_type, x.text, idempotency_key=x.idempotency_key
        )
    except RuntimeError as exc:
        raise HTTPException(502, str(exc)) from exc
    except Exception:
        logger.exception("movie_feedback_failed tmdb_id=%s user_id=%s", tmdb_id, u.id)
        raise HTTPException(502, "Could not save feedback.") from None


async def saved_movie_v1(tmdb_id: int, u=Depends(user_dep)):
    return saved_movie(tmdb_id, u)


def saved_movie(tmdb_id: int, u=Depends(user_dep)):
    db = SessionLocal()
    try:
        movie = db.scalar(select(Movie).where(Movie.tmdb_id == tmdb_id))
        if movie is None:
            raise HTTPException(404, "Movie not in local state yet") from None
        return movie.metadata_json or {
            "tmdb_id": movie.tmdb_id,
            "title": movie.title,
        }
    finally:
        db.close()


def ingest_v1(x: IngestIn, u=Depends(user_dep)):
    return ingest(x, u)


def ingest(x: IngestIn, u=Depends(user_dep)):
    if not settings.allow_user_rag_ingest or getattr(u, "status", "") != "admin":
        raise HTTPException(403, "Knowledge ingestion is disabled for application users.") from None
    db = SessionLocal()
    try:
        d = RAGService(db).ingest(**x.model_dump())
        return {"id": d.id}
    except Exception:
        logger.exception("rag_ingest_failed user_id=%s source=%s", u.id, x.source)
        raise HTTPException(502, "Could not ingest the document.") from None
    finally:
        db.close()


def _register_versioned_routers():
    from app.routers.chat import router as chat_router
    from app.routers.conversations import router as conversations_router
    from app.routers.health import router as health_router
    from app.routers.me import router as me_router
    from app.routers.movies import router as movies_router
    from app.routers.people import router as people_router
    from app.routers.projects import router as projects_router
    from app.routers.push import router as push_router
    from app.routers.sharing import router as sharing_router

    for versioned_router in (
        health_router,
        chat_router,
        conversations_router,
        movies_router,
        people_router,
        me_router,
        sharing_router,
        projects_router,
        push_router,
    ):
        app.include_router(versioned_router)


_register_versioned_routers()
