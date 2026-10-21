import secrets
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_BACKEND_DIR = Path(__file__).resolve().parents[1]
_ROOT_DIR = _BACKEND_DIR.parent


def _normalize_database_url(database_url: str) -> str:
    if database_url.startswith("sqlite+aiosqlite://"):
        return "sqlite://" + database_url[len("sqlite+aiosqlite://") :]
    if database_url.startswith("postgresql+asyncpg://"):
        return "postgresql+psycopg://" + database_url[len("postgresql+asyncpg://") :]
    if database_url.startswith("postgresql://"):
        return "postgresql+psycopg://" + database_url[len("postgresql://") :]
    if database_url.startswith("postgresql+psycopg2://"):
        return "postgresql+psycopg://" + database_url[len("postgresql+psycopg2://") :]
    return database_url


class Settings(BaseSettings):
    app_name: str = "Grace"
    app_version: str = "5.1.0"

    # `environment` is the single source of truth for "are we in production".
    # Previously this was inferred from the database URL (sqlite vs not),
    # which meant a SQLite deployment silently skipped every production
    # safety check (JWT secret, required API keys). Set ENVIRONMENT=production
    # explicitly to enable those checks regardless of which database is used.
    environment: str = "development"

    database_url: str = "postgresql+psycopg://movie_agent:movie_agent@localhost:5432/movie_agent"

    tmdb_api_key: str = ""
    omdb_api_key: str | None = None
    groq_api_key: str | None = None
    gemini_api_key: str | None = None
    # Web Push (opt-in device notifications). Generate a pair with
    # `python -m app.services.web_push --generate-keys`; unset turns push off.
    vapid_public_key: str | None = None
    vapid_private_key: str | None = None
    vapid_subject: str = "mailto:admin@example.com"
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_email: str | None = None
    smtp_password: str = ""

    groq_model: str = "openai/gpt-oss-20b"
    groq_deep_model: str = "openai/gpt-oss-120b"
    groq_vision_model: str = "qwen/qwen3.8-27b"
    # The model for rolling summaries and memory extraction after a reply.
    # The Llama ids used before (llama-3.1-8b-instant, llama-3.3-70b-versatile)
    # answered 404 for the owner's key in the live check of 2026-10-13, so the
    # summary role uses the fast model, which that check verified.
    groq_summary_model: str = "openai/gpt-oss-20b"
    # Tried in order when the role's own models are rate limited or unavailable.
    # Groq limits are per model (8,000 tokens a minute each on the free tier, as
    # Groq's headers reported on 2026-10-13), so another model adds headroom.
    # qwen/qwen3.8-27b is in the key's model list and answered the live check;
    # its tool calling is checked by the live check too. Override with
    # GROQ_FALLBACK_MODELS=model-a,model-b, or set it empty for none.
    groq_fallback_models: str = "qwen/qwen3.8-27b"
    # "gemini-2.5-flash" is Google's own documented, actively-deprecated
    # 2025-generation model; per the Gemini API deprecations page, new
    # projects are now directed to "3.5 Flash-Lite or 3.8 Flash". A 404
    # ("not_found", see upstream_errors.classify_upstream_exception) on a
    # grounded-search call that never changes shape is the exact symptom
    # of a retired model id, so the default follows that guidance; it
    # remains fully overridable via the GEMINI_MODEL environment variable
    # if a given API key is pinned to a different model.
    gemini_model: str = "gemini-3.8-flash"
    # Tried in order when GEMINI_MODEL answers 404 (retired), 5xx (overloaded) or
    # 429 (that model's quota). gemini-3.8-flash answered 503 or 504 in all four live
    # runs (2026-10-13/14). gemini-2.5-flash is in that key's model list but answered
    # 404, so it is not used; a "lite" model is tried because every full flash model
    # answered 503/504 (not yet verified to work). Override with
    # GEMINI_FALLBACK_MODELS=model-a,model-b, or set it empty for none.
    gemini_fallback_models: str = "gemini-3.7-flash,gemini-flash-lite-latest,gemini-3.5-flash"

    jwt_secret: str = ""
    jwt_signing_key: str | None = None
    otp_pepper: str | None = None
    csrf_key: str | None = None
    metrics_salt: str | None = None
    jwt_algorithm: str = "HS256"
    access_token_ttl_minutes: int = 15
    refresh_token_ttl_days: int = 30
    cookie_max_age_seconds: int = 30 * 24 * 60 * 60
    cookie_secure: bool | None = None
    otp_expire_minutes: int = 5

    cors_origins: str = "http://localhost:5173"
    cors_origin_regex: str = ""

    # Per-attempt timeout for one TMDB HTTP call; _get_with_retries() makes up
    # to 3 attempts, so the effective worst-case ceiling is larger than this
    # (see tmdb_client.py). 3.0s was too tight for any connection with real
    # round-trip latency and left the retry loop almost no room to retry.
    tmdb_timeout_seconds: float = 6.0
    # get_movie_details() bundles nine sub-resources into one response via
    # append_to_response (credits, keywords, images, videos, reviews,
    # release_dates, external_ids, translations, watch/providers,
    # recommendations) -- a structurally much larger payload than any other
    # TMDB call this client makes, and the one call observed burning all 3
    # retries' worth of tmdb_timeout_seconds under real network conditions
    # that a bare connectivity check does not reproduce. Retrying faster
    # does not help a request that genuinely needs more time to transfer;
    # only this specific, heavier call gets the longer per-attempt budget.
    tmdb_details_timeout_seconds: float = 12.0
    # The connect phase (DNS + TCP + TLS handshake) and the read phase (the
    # server actually sending a response) fail for different reasons and
    # deserve different budgets. A route that is genuinely unreachable
    # (e.g. no VPN on a network path that blocks TMDB) fails or hangs at
    # connect, almost always well under a few seconds once it is truly
    # dead -- there is no reason to wait as long as tmdb_timeout_seconds
    # for that, and no benefit to retrying it 3 times at that same long
    # budget (observed: ~37s total for a provably dead route, all spent on
    # a connect-phase failure that was never going to succeed no matter
    # how long it waited). A connection that DID establish but is slow to
    # respond is a different, more plausibly transient failure and keeps
    # the full tmdb_timeout_seconds/tmdb_details_timeout_seconds budget.
    # This is wired in as httpx.Timeout(connect=..., read=<per-attempt>, ...)
    # rather than one blanket float -- see TMDBClient._get_with_retries.
    tmdb_connect_timeout_seconds: float = 5.0
    # Optional, owner-configured outbound proxy for TMDB traffic only (e.g.
    # a small VPS the owner controls, outside a network path that cannot
    # reach TMDB directly). Empty by default: no proxy is used unless this
    # is explicitly set. Never logged (see tmdb_client.py), never applied
    # to any other provider, and never defaulted to a public or
    # third-party proxy -- the owner supplies and is responsible for it.
    tmdb_proxy_url: str | None = None
    default_region: str = "NL"
    web_timeout_seconds: float = 4.0
    # A Gemini call that asks the model to use Google Search grounding: the
    # server performs the search and composes the answer in one round trip,
    # which reliably takes longer than a plain (non-grounded) generation call.
    gemini_grounding_timeout_seconds: float = 10.0
    # SPARQL queries against the public query.wikidata.org endpoint (and the
    # lighter wbsearchentities lookups) routinely take several seconds; 1.0s
    # made every non-trivial query time out regardless of whether the data
    # existed.
    # 8.0s was too tight in practice: a 2026-10-09 VPN-off/Cloudflare-DNS
    # live run (TMDB and everything else passing cleanly) still saw
    # Wikidata pass only after "multiple timeout/retry events" totalling
    # 16.8s -- consistent with a query that genuinely needs a bit over 8s
    # to complete on a cold hit, timing out on attempt 1 (losing that
    # ~8-9s outright) and then succeeding on attempt 2 within its own
    # budget. query_sparql()'s retry-on-timeout (one retry, unchanged)
    # already helps correctness, but it cannot help LATENCY when the
    # first attempt's budget was simply too small for a query that was
    # always going to succeed, just not within 8s -- that case pays for
    # two attempts' worth of waiting where one, slightly longer attempt
    # would have done. Raised to 10.0, informed directly by this run,
    # not a guess: still a single bounded per-attempt budget, and the
    # retry count (2 attempts total, see query_sparql) is unchanged.
    wikidata_timeout_seconds: float = 10.0
    cache_ttl_seconds: int = 300
    cache_max_entries: int = 1000
    # Award/location facts for a given film are effectively static (a film's
    # Wikidata awards rarely change), unlike the 5-minute web-search cache
    # above, so a successful SPARQL result is worth keeping much longer --
    # both to avoid re-querying the same film repeatedly and so a later,
    # transient timeout can fall back to the last known-good answer instead
    # of an empty result (see WikidataClient.query_sparql).
    wikidata_cache_ttl_seconds: int = 21600

    llm_timeout_seconds: float = 15.0
    llm_tool_max_output_tokens: int = 700
    llm_answer_max_output_tokens: int = 1200
    # Model-led turns (tool calling). One answer may be long (a story, an
    # itinerary); a reply cut at this limit is continued once automatically.
    llm_agent_max_output_tokens: int = 1600
    # How much conversation (in estimated tokens) the answering model receives.
    # Free Groq tiers allow roughly 6-8k tokens per minute per model, so the
    # whole request (prompt, tools, context, history) is kept well under that.
    llm_history_token_budget: int = 2600
    route_deadline_seconds: float = 4.0
    tool_timeout_seconds: float = 5.0
    # Room to wait out a per-minute limit on the chat model (up to 45 s) and still answer.
    agent_deadline_seconds: float = 75.0
    forwarded_allow_ips: str = "127.0.0.1,::1"

    max_agent_rounds: int = 3
    max_tool_calls: int = 10
    max_web_searches: int = 4
    # Budget for explicit multi-domain research operations (movie intelligence pages).
    max_research_web_searches: int = 8
    max_research_tool_calls: int = 40
    max_tool_concurrency: int = 4
    max_history_messages: int = 30
    max_memory_results: int = 8
    max_context_chars: int = 24000
    max_tool_result_chars: int = 10000

    login_rate_limit: int = 12
    register_rate_limit: int = 5
    chat_rate_limit: int = 30
    chat_ip_rate_limit: int = 20
    auth_hash_concurrency: int = 4
    account_deletion_grace_days: int = 14
    allow_user_rag_ingest: bool = False

    # Optional shared state backend. When unset, rate limiting and short-lived
    # caches fall back to in-process memory, which only works correctly with
    # a single worker process / single instance. Set this for any deployment
    # that runs more than one uvicorn worker or more than one replica.
    redis_url: str | None = None
    sentry_dsn: str | None = None
    sentry_release: str | None = None
    otel_exporter_otlp_endpoint: str | None = None
    otel_exporter_otlp_traces_endpoint: str | None = None
    metrics_bearer_token: str | None = None
    metrics_user_salt: str | None = None
    session_idle_timeout_hours: int = 24 * 7
    session_absolute_timeout_days: int = 30
    max_sessions_per_user: int = 10
    llm_input_cost_per_1m_usd: float = 0.0
    export_dir: str = str(Path(tempfile.gettempdir()) / "grace-exports")
    export_max_bytes: int = 50 * 1024 * 1024
    export_retention_hours: int = 24
    export_poll_seconds: float = 2.0
    db_pool_size: int = 5
    db_max_overflow: int = 5
    db_worker_count: int = 2
    db_replica_count: int = 1
    db_connection_budget: int = 20
    llm_output_cost_per_1m_usd: float = 0.0

    model_config = SettingsConfigDict(
        env_file=(str(_ROOT_DIR / ".env"), str(_BACKEND_DIR / ".env")),
        extra="ignore",
    )

    @model_validator(mode="after")
    def validate_security_configuration(self):
        if (
            self.db_pool_size < 1
            or self.db_max_overflow < 0
            or self.db_worker_count < 1
            or self.db_replica_count < 1
        ):
            raise ValueError("Database pool, worker, and replica counts must be positive.")
        estimated_connections = (
            self.db_replica_count * self.db_worker_count * (self.db_pool_size + self.db_max_overflow)
        )
        if estimated_connections > self.db_connection_budget:
            raise ValueError(
                "Configured PostgreSQL connection budget is too small: "
                f"need {estimated_connections}, budget {self.db_connection_budget}."
            )
        if self.is_production:
            required = {
                "JWT_SIGNING_KEY": self.jwt_signing_key or self.jwt_secret,
                "OTP_PEPPER": self.otp_pepper,
                "CSRF_KEY": self.csrf_key,
                "METRICS_SALT": self.metrics_salt or self.metrics_user_salt,
                "REDIS_URL": self.redis_url,
                "METRICS_BEARER_TOKEN": self.metrics_bearer_token,
            }
            missing = [
                name
                for name, value in required.items()
                if not value or (name != "REDIS_URL" and len(str(value)) < 32)
            ]
            if missing:
                raise ValueError(f"Production security configuration is incomplete: {', '.join(missing)}")
            if not self.cors_origins.strip() or self.cors_origin_regex:
                raise ValueError("Production requires explicit CORS_ORIGINS and no CORS_ORIGIN_REGEX.")
        return self

    def model_post_init(self, _context) -> None:
        if not self.is_production and not self.has_configured_jwt_secret:
            self.jwt_secret = secrets.token_urlsafe(48)
        if not self.is_production and not self.cors_origin_regex:
            self.cors_origin_regex = r"^https?://(localhost|127\.0\.0\.1)(:\d+)?$"

    @property
    def is_production(self) -> bool:
        return self.environment.strip().lower() == "production"

    @property
    def cookie_secure_enabled(self) -> bool:
        return self.cookie_secure if self.cookie_secure is not None else self.is_production

    @property
    def allowed_cors_origins(self) -> list[str]:
        configured = [origin.strip().rstrip("/") for origin in self.cors_origins.split(",") if origin.strip()]
        loopback_aliases = [
            "http://localhost",
            "http://localhost:80",
            "http://127.0.0.1",
            "http://127.0.0.1:80",
        ]
        if self.is_production:
            # Production never invents origins. The only additions are the
            # port-80 aliases of an explicitly configured loopback origin,
            # which is how the bundled Compose frontend is reached locally.
            if any(urlsplit(origin).hostname in {"localhost", "127.0.0.1"} for origin in configured):
                return list(dict.fromkeys([*configured, *loopback_aliases]))
            return configured
        local_http_origins = [
            *loopback_aliases,
            "http://localhost:5173",
            "http://localhost:4173",
            "http://127.0.0.1:5173",
            "http://127.0.0.1:4173",
            "https://localhost",
            "https://127.0.0.1",
        ]
        return list(dict.fromkeys([*configured, *local_http_origins]))

    @property
    def has_configured_jwt_secret(self) -> bool:
        secret = (self.jwt_signing_key or self.jwt_secret).strip()
        placeholder_markers = (
            "change-me",
            "replace-with",
            "supersecret",
            "your-secret",
        )
        return len(secret) >= 32 and not any(marker in secret.lower() for marker in placeholder_markers)


settings = Settings()

# The service layer uses synchronous SQLAlchemy sessions and drivers.
settings.database_url = _normalize_database_url(settings.database_url)
