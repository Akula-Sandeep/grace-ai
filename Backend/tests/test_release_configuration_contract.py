from pathlib import Path

from app.config import Settings

ROOT = Path(__file__).resolve().parents[2]
FRONTEND_HEADERS = ROOT / "Frontend" / "nginx-security-headers.conf"
FRONTEND_PACKAGE = ROOT / "Frontend" / "package.json"
CI_WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"


def test_production_cors_does_not_add_development_origins():
    production = Settings(
        environment="production",
        cors_origins="https://grace.example",
        cors_origin_regex="",
        database_url="sqlite:///./contract-test.db",
        jwt_secret="x" * 64,
        otp_pepper="x" * 64,
        csrf_key="x" * 64,
        metrics_salt="x" * 64,
        metrics_bearer_token="x" * 64,
        redis_url="redis://localhost:6379/0",
    )

    assert production.allowed_cors_origins == ["https://grace.example"]
    assert all("localhost" not in origin for origin in production.allowed_cors_origins)
    assert all("127.0.0.1" not in origin for origin in production.allowed_cors_origins)


def test_token_and_cookie_lifetime_settings_are_distinct():
    settings = Settings(database_url="sqlite:///./contract-test.db")
    assert settings.access_token_ttl_minutes == 15
    assert settings.refresh_token_ttl_days == 30
    assert settings.cookie_max_age_seconds == 30 * 24 * 60 * 60
    assert settings.session_idle_timeout_hours == 24 * 7
    assert settings.session_absolute_timeout_days == 30


def test_security_headers_remove_hsts_and_inline_styles_but_allow_configured_sentry():
    headers = FRONTEND_HEADERS.read_text()
    assert "Strict-Transport-Security" not in headers
    assert "style-src 'self'" in headers
    assert "style-src 'unsafe-inline'" not in headers
    assert "connect-src 'self' https://*.sentry.io https://*.ingest.sentry.io" in headers


def test_frontend_backend_release_identity_is_aligned():
    import json

    package = json.loads(FRONTEND_PACKAGE.read_text())
    backend = Settings(database_url="sqlite:///./contract-test.db")
    workflow = CI_WORKFLOW.read_text()

    assert package["version"] == backend.app_version == "5.1.0"
    assert "VITE_SENTRY_RELEASE: grace@5.1.0" in workflow
    assert "SENTRY_RELEASE: grace@5.1.0" in workflow
    assert "id: sentry_release" in workflow
    assert "if: steps.sentry_release.outputs.enabled == 'true'" in workflow
    assert "if: ${{ env.SENTRY_AUTH_TOKEN !=" not in workflow


def test_connection_budget_contract_is_present_in_deployment():
    compose = (ROOT / "docker-compose.yml").read_text()
    assert "DB_CONNECTION_BUDGET" in compose
    assert "DB_POOL_SIZE" in compose
    assert "DB_MAX_OVERFLOW" in compose
    assert "DB_WORKER_COUNT" in compose
    assert "DB_REPLICA_COUNT" in compose


def test_migrations_run_in_a_dedicated_job_and_runtime_workers_use_app_role():
    compose = (ROOT / "docker-compose.yml").read_text()
    dockerfile = (ROOT / "Backend" / "Dockerfile").read_text()

    assert "db-migrate:" in compose
    assert "MIGRATOR_DB_USER" in compose
    assert "DATABASE_URL: postgresql+psycopg://${MIGRATOR_DB_USER" in compose
    assert "APP_DB_USER" in compose
    assert "DATABASE_URL: postgresql+psycopg://${APP_DB_USER" in compose
    assert 'command: ["alembic", "upgrade", "head"]' in compose
    assert "alembic upgrade head" not in dockerfile


def test_pgvector_is_mandatory_for_migrations_but_embedding_models_remain_optional():
    runtime_lock = (ROOT / "Backend" / "requirements-runtime.lock").read_text()
    embeddings_in = (ROOT / "Backend" / "requirements-embeddings.in").read_text()
    embeddings_lock = (ROOT / "Backend" / "requirements-embeddings.lock").read_text()

    assert "pgvector==0.5.0" in runtime_lock
    assert "pgvector==0.5.0" not in embeddings_in
    assert "pgvector==0.5.0" not in embeddings_lock
    assert "sentence-transformers==6.1.0" in embeddings_in


def test_embeddings_are_optional_in_the_default_backend_image():
    dockerfile = (ROOT / "Backend" / "Dockerfile").read_text()
    assert "ARG INSTALL_EMBEDDINGS=false" in dockerfile
    assert "requirements-runtime.lock" in dockerfile
    assert "requirements-embeddings.lock" in dockerfile
    assert 'if [ "$INSTALL_EMBEDDINGS" = "true" ]' in dockerfile


def test_local_caches_have_explicit_bounds(monkeypatch):
    import asyncio

    from app.services.async_cache import LoopScopedTTLCache
    from app.services.tmdb_client import TMDBClient
    from app.services.web_search_client import WebSearchClient

    monkeypatch.setattr(Settings(), "cache_max_entries", 2, raising=False)
    monkeypatch.setattr("app.services.web_search_client.settings.cache_max_entries", 2)
    monkeypatch.setattr("app.services.tmdb_client.settings.cache_max_entries", 2)

    assert LoopScopedTTLCache(ttl=10, maxsize=3).maxsize == 3
    TMDBClient._cache.clear()
    for index in range(3):
        TMDBClient._cache_put(("movie", index), {"id": index})
    assert len(TMDBClient._cache) <= 2

    class Provider:
        async def search(self, query):
            return {"query": query}

        async def close(self):
            pass

    async def exercise_web_cache():
        client = WebSearchClient(provider=Provider())
        client._cache.clear()
        for index in range(3):
            await client.search(f"unique-cache-query-{index}")
        return len(client._cache)

    assert asyncio.run(exercise_web_cache()) <= 2


def test_state_ownership_contract_is_documented():
    readme = (ROOT / "README.md").read_text()
    assert "### Frontend state ownership contract" in readme
    assert "Server-authoritative state" in readme
    assert "Session-scoped local state" in readme
    assert "Request-control state" in readme


def test_release_artifact_hygiene_contract_is_present():
    script = (ROOT / "scripts" / "build_release_artifact.py").read_text()
    assert "node_modules" in script
    assert "test-results" in script
    assert ".env" in script
    assert "SECRET_ASSIGNMENT" in script
    assert "looks_like_placeholder" in script


def test_alembic_revision_ids_fit_default_version_column_width():
    import re

    versions_dir = ROOT / "Backend" / "alembic" / "versions"
    revision_ids = []
    for path in versions_dir.glob("*.py"):
        match = re.search(r"^revision\s*=\s*[\"\']([^\"\']+)[\"\']", path.read_text(), re.MULTILINE)
        if match:
            revision_ids.append((path.name, match.group(1)))

    assert revision_ids
    assert all(len(revision) <= 32 for _, revision in revision_ids)
