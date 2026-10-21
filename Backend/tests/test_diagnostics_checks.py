"""The individual checks of the live check (services/diagnostics.py), with providers faked.

Each check must report what it found or why it failed, by category, and never raise.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import httpx
import pytest

from app.config import settings
from app.services import diagnostics


class FakeTMDB:
    plan: dict = {}

    def __init__(self, *args, **kwargs):
        self.closed = False

    async def search_movies(self, query):
        outcome = FakeTMDB.plan.get("search", {"results": [{"id": 329865, "release_date": "2016-11-10"}]})
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    async def get_movie_details(self, movie_id):
        outcome = FakeTMDB.plan.get(
            "details", {"id": movie_id, "title": "Arrival", "release_date": "2016-11-10", "runtime": 116}
        )
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    async def get_watch_providers(self, movie_id, region):
        outcome = FakeTMDB.plan.get("providers", {"flatrate": [{"provider_name": "Netflix"}]})
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    async def close(self):
        self.closed = True


@pytest.fixture
def tmdb(monkeypatch):
    from app.services import tmdb_client

    monkeypatch.setattr(settings, "tmdb_api_key", "test-key")
    monkeypatch.setattr(settings, "default_region", "NL")
    monkeypatch.setattr(tmdb_client, "TMDBClient", FakeTMDB)
    FakeTMDB.plan = {}
    return FakeTMDB


def _by_service(results):
    return {r["service"]: r for r in results}


@pytest.mark.asyncio
async def test_tmdb_search_details_and_providers_are_each_reported(tmdb):
    results = _by_service(await diagnostics._check_tmdb())
    assert results["TMDB search"]["ok"] and results["TMDB search"]["detail"] == "1 results."
    assert results["TMDB details"]["detail"] == "Arrival (2016), 116 min."
    assert results["TMDB watch providers (NL)"]["detail"] == "Netflix"


@pytest.mark.asyncio
async def test_tmdb_without_results_stops_after_the_search(tmdb):
    tmdb.plan = {"search": {"results": []}}
    results = await diagnostics._check_tmdb()
    assert [(r["service"], r["ok"], r["detail"]) for r in results] == [("TMDB search", False, "No results.")]


@pytest.mark.asyncio
async def test_a_failing_tmdb_step_is_a_failed_result_and_the_next_still_runs(tmdb):
    tmdb.plan = {"details": httpx.ReadTimeout("slow"), "providers": {}}
    results = _by_service(await diagnostics._check_tmdb())
    assert results["TMDB details"]["ok"] is False and results["TMDB details"]["category"] == "timeout"
    assert results["TMDB watch providers (NL)"]["detail"] == "No subscription services listed."
    tmdb.plan = {"search": httpx.ConnectError("[Errno 111] Connection refused")}
    (only,) = await diagnostics._check_tmdb()
    assert only["service"] == "TMDB search" and only["category"] == "connect_error"
    tmdb.plan = {"search": {"results": [{"id": 1}]}, "providers": httpx.ReadTimeout("slow")}
    results = _by_service(await diagnostics._check_tmdb())
    assert results["TMDB watch providers"]["ok"] is False


@pytest.mark.asyncio
async def test_tmdb_without_a_key_is_not_configured(monkeypatch):
    monkeypatch.setattr(settings, "tmdb_api_key", "")
    (result,) = await diagnostics._check_tmdb()
    assert result["category"] == "not_configured" and result["status"] == "not_configured" and result["hint"]


def test_gemini_candidates_put_stable_flash_models_first(monkeypatch):
    monkeypatch.setattr(settings, "gemini_model", "gemini-3.8-flash")
    monkeypatch.setattr(settings, "gemini_fallback_models", "gemini-3.7-flash, gemini-3.7-flash")
    names = [
        "gemini-omni-flash-preview",
        "gemini-2.5-flash",
        "gemini-3.8-flash",  # configured: not an alternative
        "gemini-flash-latest",
        "gemini-3.5-flash",
        "gemini-flash-lite-latest",
        "gemini-pro",
    ]
    assert diagnostics._gemini_candidates(names) == [
        "gemini-flash-latest",
        "gemini-3.5-flash",
        "gemini-2.5-flash",
        "gemini-omni-flash-preview",
        "gemini-flash-lite-latest",
    ]
    assert diagnostics._configured_fallbacks() == ["gemini-3.7-flash"]


class _Pager:
    def __init__(self, models):
        self.models = models

    def __aiter__(self):
        async def gen():
            for model in self.models:
                yield model

        return gen()


@pytest.mark.asyncio
async def test_gemini_alternatives_lists_text_models_and_tries_the_best_two(monkeypatch):
    monkeypatch.setattr(settings, "gemini_model", "gemini-3.8-flash")
    monkeypatch.setattr(settings, "gemini_fallback_models", "")
    listed = [
        SimpleNamespace(name="models/gemini-flash-latest", supported_actions=["generateContent"]),
        SimpleNamespace(name="models/gemini-3.5-flash", supported_actions=["generateContent"]),
        SimpleNamespace(name="models/gemini-2.5-flash", supported_actions=["generateContent"]),
        SimpleNamespace(name="models/text-embedding-004", supported_actions=["embedContent"]),
    ]
    tried = []

    async def generate_content(*, model, contents):
        tried.append(model)
        if model == "gemini-3.5-flash":
            raise httpx.ReadTimeout("slow")
        return SimpleNamespace(text="OK")

    async def list_models():
        return _Pager(listed)

    client = SimpleNamespace(
        aio=SimpleNamespace(models=SimpleNamespace(list=list_models, generate_content=generate_content))
    )
    results = await diagnostics._gemini_alternatives(client)
    assert results[0]["detail"] == "3 text models listed."
    assert tried == ["gemini-flash-latest", "gemini-3.5-flash"]
    assert results[1]["ok"] and "could be used as GEMINI_MODEL" in results[1]["detail"]
    assert results[2]["ok"] is False and results[2]["category"] == "timeout"

    async def broken_list():
        raise httpx.ConnectError("[Errno 111] Connection refused")

    client.aio.models.list = broken_list
    (failed,) = await diagnostics._gemini_alternatives(client)
    assert failed["ok"] is False and failed["category"] == "connect_error"


@pytest.mark.asyncio
async def test_a_check_that_raises_or_is_cancelled_becomes_its_own_failed_result():
    async def broken():
        raise ValueError("boom")

    (failed,) = await diagnostics._settle("TMDB search", broken())
    assert failed["ok"] is False and failed["service"] == "TMDB search"

    async def cancelled():
        raise asyncio.CancelledError

    (stopped,) = await diagnostics._settle("Gemini", cancelled())
    assert stopped["category"] == "timeout" and stopped["detail"] == "Cancelled."

    async def single():
        return {"service": "x", "ok": True}

    assert await diagnostics._settle("x", single()) == [{"service": "x", "ok": True}]


def test_error_codes_report_structured_fields_only_never_the_message():
    class Quota(Exception):
        status_code = 429
        status = "RESOURCE_EXHAUSTED"
        details = {
            "error": {
                "details": [
                    {
                        "@type": "type.googleapis.com/google.rpc.QuotaFailure",
                        "violations": [{"quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier"}],
                    }
                ]
            }
        }
        body = {"error": {"code": "rate_limit_exceeded"}}

    codes = diagnostics._error_codes(Quota("secret request text"))
    assert codes[:3] == ["429", "RESOURCE_EXHAUSTED", "GenerateRequestsPerDayPerProjectPerModel-FreeTier"]
    assert not any("secret" in code for code in codes)

    class Wrapper(Exception):
        pass

    wrapped = Wrapper("wrapped")
    wrapped.__cause__ = Quota("x")
    assert "429" in diagnostics._error_codes(wrapped)
    assert diagnostics._quota_id(ValueError("x")) is None


def test_sdk_versions_name_every_provider_sdk():
    versions = diagnostics._sdk_versions()
    assert set(versions) == {"groq", "google-genai", "httpx"}
