from types import SimpleNamespace
from typing import ClassVar

import pytest

from app.agent.router import QueryClassification
from app.config import settings
from app.services.movie_data_fallback import MovieDataFallbackEngine


class FakeTMDB:
    def __init__(self, runtime):
        self.runtime = runtime

    async def search_movies(self, title):
        return {
            "results": [
                {
                    "id": 1,
                    "title": title,
                    "original_title": title,
                }
            ]
        }

    async def get_movie_details(self, movie_id):
        return {"id": movie_id, "title": "Baahubali", "runtime": self.runtime}


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self.payload


class FakeHTTPClient:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []

    async def get(self, url, params=None, **kwargs):
        self.calls.append((url, params, kwargs))
        return next(self.responses)


@pytest.mark.asyncio
async def test_runtime_uses_tmdb_when_runtime_is_positive(monkeypatch):
    monkeypatch.setattr(settings, "omdb_api_key", "test-omdb")
    http = FakeHTTPClient([])
    engine = MovieDataFallbackEngine(tmdb=FakeTMDB(180), client=http)

    result = await engine.lookup_runtime("Baahubali")

    assert result.status == "found"
    assert result.runtime == 180
    assert result.source == "TMDB"
    assert http.calls == []


@pytest.mark.asyncio
async def test_movie_search_accepts_a_unique_subtitle_alias_from_tmdb():
    class AliasTMDB:
        calls: ClassVar[list] = []

        async def _get(self, path, params):
            self.calls.append((path, params))
            return {
                "results": [
                    {
                        "id": 1087046,
                        "title": "Devara: Part 1",
                        "original_title": "Devara: Part 1",
                        "release_date": "2024-09-27",
                    }
                ]
            }

        async def get_movie_details(self, movie_id):
            assert movie_id == 1087046
            return {
                "id": movie_id,
                "title": "Devara: Part 1",
                "release_date": "2024-09-27",
                "runtime": 175,
            }

    tmdb = AliasTMDB()
    engine = MovieDataFallbackEngine(tmdb=tmdb, client=FakeHTTPClient([]))

    result = await engine.resolve_movie("Devara")

    assert result["status"] == "found"
    assert result["details"]["title"] == "Devara: Part 1"
    assert tmdb.calls[0][1]["include_adult"] is False
    assert len(tmdb.calls) == 1


@pytest.mark.asyncio
async def test_runtime_falls_back_to_omdb_when_tmdb_runtime_is_zero(monkeypatch):
    monkeypatch.setattr(settings, "omdb_api_key", "test-omdb")
    http = FakeHTTPClient([FakeResponse({"Response": "True", "Title": "Baahubali", "Runtime": "159 min"})])
    engine = MovieDataFallbackEngine(tmdb=FakeTMDB(0), client=http)

    result = await engine.lookup_runtime("Baahubali")

    assert result.status == "found"
    assert result.runtime == 159
    assert result.source == "OMDb"
    assert http.calls[0][1]["apikey"] == "test-omdb"


@pytest.mark.asyncio
async def test_runtime_continues_to_wikipedia_when_omdb_has_no_runtime(monkeypatch):
    monkeypatch.setattr(settings, "omdb_api_key", "test-omdb")
    http = FakeHTTPClient(
        [
            FakeResponse({"Response": "True", "Title": "Baahubali", "Runtime": "N/A"}),
            FakeResponse(
                {
                    "query": {
                        "pages": [
                            {
                                "title": "Baahubali (film)",
                                "revisions": [{"slots": {"main": {"content": "| runtime = 159 minutes"}}}],
                            }
                        ]
                    }
                }
            ),
        ]
    )
    engine = MovieDataFallbackEngine(tmdb=FakeTMDB(None), client=http)

    result = await engine.lookup_runtime("Baahubali")

    assert result.status == "found"
    assert result.runtime == 159
    assert result.source == "Wikipedia"


@pytest.mark.asyncio
async def test_runtime_asks_for_disambiguation_when_tmdb_has_multiple_exact_matches():
    class AmbiguousTMDB:
        async def search_movies(self, title):
            return {
                "results": [
                    {"id": 1, "title": title},
                    {"id": 2, "title": title},
                ]
            }

        async def get_movie_details(self, movie_id):
            raise AssertionError("Ambiguous title should not fetch details.")

    engine = MovieDataFallbackEngine(tmdb=AmbiguousTMDB(), client=FakeHTTPClient([]))

    result = await engine.lookup_runtime("A Film")

    assert result.status == "ambiguous"
    assert result.candidates == ["A Film", "A Film"]


@pytest.mark.asyncio
async def test_gemini_level_requires_google_search_grounding(monkeypatch):
    from google import genai

    monkeypatch.setattr(settings, "gemini_api_key", "test-gemini")
    captured = {}

    class FakeClient:
        def __init__(self, **_kwargs):
            self.aio = SimpleNamespace(
                models=SimpleNamespace(generate_content=self.generate_content),
                aclose=self.aclose,
            )

        async def generate_content(self, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(
                text="RUNTIME: 159",
                candidates=[
                    SimpleNamespace(
                        grounding_metadata=SimpleNamespace(
                            grounding_chunks=[
                                SimpleNamespace(web=SimpleNamespace(uri="https://example.com/runtime"))
                            ]
                        )
                    )
                ],
            )

        async def aclose(self):
            pass

    monkeypatch.setattr(genai, "Client", FakeClient)
    engine = MovieDataFallbackEngine(tmdb=FakeTMDB(None), client=FakeHTTPClient([]))
    failures = []

    result = await engine._gemini_runtime("Baahubali", failures)

    assert result == (159, "Baahubali", ["https://example.com/runtime"])
    assert captured["config"].tools[0].google_search is not None
    assert failures == []


@pytest.mark.asyncio
async def test_grounded_search_returns_only_gemini_results_with_source_urls(
    monkeypatch,
):
    from google import genai

    monkeypatch.setattr(settings, "gemini_api_key", "test-gemini")
    captured = {}

    class FakeClient:
        def __init__(self, **_kwargs):
            self.aio = SimpleNamespace(
                models=SimpleNamespace(generate_content=self.generate_content),
                aclose=self.aclose,
            )

        async def generate_content(self, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(
                text='{"answer":"A current, search-grounded report."}',
                candidates=[
                    SimpleNamespace(
                        grounding_metadata=SimpleNamespace(
                            grounding_chunks=[
                                SimpleNamespace(web=SimpleNamespace(uri="https://example.com/news"))
                            ]
                        )
                    )
                ],
            )

        async def aclose(self):
            pass

    monkeypatch.setattr(genai, "Client", FakeClient)
    engine = MovieDataFallbackEngine(tmdb=FakeTMDB(None), client=FakeHTTPClient([]))

    result = await engine.grounded_search("upcoming film release")

    assert result["summary"] == "A current, search-grounded report."
    assert result["sources"] == ["https://example.com/news"]
    assert result["source_details"][0]["source_domain"] == "example.com"
    assert captured["config"].tools[0].google_search is not None
    # The search tool is the evidence contract; no JSON response body is requested.
    assert getattr(captured["config"], "response_mime_type", None) in (None, "")


@pytest.mark.asyncio
async def test_grounded_search_resolves_redirect_publishers_and_supported_segments(
    monkeypatch,
):
    from google import genai

    monkeypatch.setattr(settings, "gemini_api_key", "test-gemini")
    redirect = "https://vertexaisearch.cloud.google.com/grounding-api-redirect/abc"

    class FakeClient:
        def __init__(self, **_kwargs):
            self.aio = SimpleNamespace(
                models=SimpleNamespace(generate_content=self.generate_content),
                aclose=self.aclose,
            )

        async def generate_content(self, **_kwargs):
            return SimpleNamespace(
                text="The film was shot in Jordan. Some reports also mention the UAE.",
                candidates=[
                    SimpleNamespace(
                        grounding_metadata=SimpleNamespace(
                            grounding_chunks=[
                                SimpleNamespace(web=SimpleNamespace(uri=redirect, title="variety.com")),
                                SimpleNamespace(
                                    web=SimpleNamespace(uri=redirect + "2", title="fan-wiki.example")
                                ),
                            ],
                            grounding_supports=[
                                SimpleNamespace(
                                    segment=SimpleNamespace(text="The film was shot in Jordan."),
                                    grounding_chunk_indices=[0],
                                ),
                                SimpleNamespace(
                                    segment=SimpleNamespace(text="Some reports also mention the UAE."),
                                    grounding_chunk_indices=[1],
                                ),
                            ],
                        )
                    )
                ],
            )

        async def aclose(self):
            pass

    monkeypatch.setattr(genai, "Client", FakeClient)
    result = await MovieDataFallbackEngine.grounded_search("where was it filmed")

    first, second = result["source_details"]
    assert (
        first["source_domain"] == "variety.com" and first["supported_text"] == "The film was shot in Jordan."
    )
    assert second["supported_text"] == "Some reports also mention the UAE."

    from app.evidence import item_tier
    from app.services.web_search_client import GeminiGroundedSearchProvider

    async def grounded(_query):
        return result

    monkeypatch.setattr(
        MovieDataFallbackEngine,
        "grounded_search",
        classmethod(lambda cls, q: grounded(q)),
    )
    web = await GeminiGroundedSearchProvider().search("where was it filmed")
    assert [item["source_domain"] for item in web["results"]] == [
        "variety.com",
        "fan-wiki.example",
    ]
    # Authority comes from the publisher, not the redirect host.
    assert item_tier(web["results"][0]) == 2 and item_tier(web["results"][1]) == 1


@pytest.mark.asyncio
async def test_grounded_search_raises_classified_timeout_instead_of_masking_it(monkeypatch):
    """A real transport failure must surface as a typed, classified error --
    never collapse into the same `None` returned for "nothing to ground"."""
    import httpx
    from google import genai

    from app.services.upstream_errors import UpstreamTimeout

    monkeypatch.setattr(settings, "gemini_api_key", "test-gemini")

    class TimingOutClient:
        def __init__(self, **_kwargs):
            self.aio = SimpleNamespace(
                models=SimpleNamespace(generate_content=self.generate_content),
                aclose=self.aclose,
            )

        async def generate_content(self, **_kwargs):
            raise httpx.ReadTimeout("simulated slow Gemini grounding response")

        async def aclose(self):
            pass

    monkeypatch.setattr(genai, "Client", TimingOutClient)

    with pytest.raises(UpstreamTimeout):
        await MovieDataFallbackEngine.grounded_search("where was it filmed")


@pytest.mark.asyncio
async def test_grounded_search_raises_classified_unavailable_for_a_server_error(monkeypatch):
    from google import genai
    from google.genai import errors as genai_errors

    from app.services.upstream_errors import UpstreamUnavailable

    monkeypatch.setattr(settings, "gemini_api_key", "test-gemini")

    class FailingClient:
        def __init__(self, **_kwargs):
            self.aio = SimpleNamespace(
                models=SimpleNamespace(generate_content=self.generate_content),
                aclose=self.aclose,
            )

        async def generate_content(self, **_kwargs):
            raise genai_errors.ServerError(503, {"error": {"message": "temporarily unavailable"}})

        async def aclose(self):
            pass

    monkeypatch.setattr(genai, "Client", FailingClient)

    with pytest.raises(UpstreamUnavailable) as excinfo:
        await MovieDataFallbackEngine.grounded_search("where was it filmed")
    # The category is safe to log and inspect; the raw provider message
    # (which may echo request text) is not folded into it.
    assert "server_error" in str(excinfo.value)


@pytest.mark.asyncio
async def test_grounded_search_surfaces_the_providers_own_rejection_detail(monkeypatch):
    """The whole point of describe_upstream_exception is that a 400/401/403
    style rejection must be diagnosable from the raised message alone --
    the provider's own code/status/message, not just a generic category.
    Without this, a client_error only ever says "client_error" and the
    actual reason (bad model name, missing grounding permission, etc.) is
    lost."""
    from google import genai
    from google.genai import errors as genai_errors

    from app.services.upstream_errors import UpstreamUnavailable

    monkeypatch.setattr(settings, "gemini_api_key", "test-gemini")

    class RejectingClient:
        def __init__(self, **_kwargs):
            self.aio = SimpleNamespace(
                models=SimpleNamespace(generate_content=self.generate_content),
                aclose=self.aclose,
            )

        async def generate_content(self, **_kwargs):
            raise genai_errors.ClientError(
                400,
                {
                    "error": {
                        "message": "The requested combination of tools is not supported.",
                        "status": "INVALID_ARGUMENT",
                    }
                },
            )

        async def aclose(self):
            pass

    monkeypatch.setattr(genai, "Client", RejectingClient)

    with pytest.raises(UpstreamUnavailable) as excinfo:
        await MovieDataFallbackEngine.grounded_search("where was it filmed")

    message = str(excinfo.value)
    assert "client_error" in message
    assert "400" in message
    assert "INVALID_ARGUMENT" in message
    assert "requested combination of tools is not supported" in message


@pytest.mark.asyncio
async def test_grounded_search_names_the_configured_model_on_a_not_found_rejection(monkeypatch):
    """A 404 on a grounded-search request whose shape never changes is
    almost always a retired or mistyped model id (see
    classify_upstream_exception's dedicated "not_found" category) --
    the raised message must point at the configured GEMINI_MODEL setting
    by name, not just report the generic status, so the fix is obvious
    without having to read this module's source."""
    from google import genai
    from google.genai import errors as genai_errors

    from app.services.upstream_errors import UpstreamUnavailable

    monkeypatch.setattr(settings, "gemini_api_key", "test-gemini")
    monkeypatch.setattr(settings, "gemini_model", "gemini-totally-retired-model")

    class NotFoundClient:
        def __init__(self, **_kwargs):
            self.aio = SimpleNamespace(
                models=SimpleNamespace(generate_content=self.generate_content),
                aclose=self.aclose,
            )

        async def generate_content(self, **_kwargs):
            raise genai_errors.ClientError(
                404,
                {
                    "error": {
                        "message": (
                            "models/gemini-totally-retired-model is not found for API version "
                            "v1beta, or is not supported for generateContent."
                        ),
                        "status": "NOT_FOUND",
                    }
                },
            )

        async def aclose(self):
            pass

    monkeypatch.setattr(genai, "Client", NotFoundClient)

    with pytest.raises(UpstreamUnavailable) as excinfo:
        await MovieDataFallbackEngine.grounded_search("where was it filmed")

    message = str(excinfo.value)
    assert "not_found" in message
    assert "gemini-totally-retired-model" in message
    assert "GEMINI_MODEL" in message


@pytest.mark.asyncio
async def test_grounded_search_returns_none_for_a_genuinely_empty_grounded_answer(monkeypatch):
    """A call that completes but grounds nothing is a different, benign
    outcome from a transport failure, and must still return `None`."""
    from google import genai

    monkeypatch.setattr(settings, "gemini_api_key", "test-gemini")

    class EmptyClient:
        def __init__(self, **_kwargs):
            self.aio = SimpleNamespace(
                models=SimpleNamespace(generate_content=self.generate_content),
                aclose=self.aclose,
            )

        async def generate_content(self, **_kwargs):
            return SimpleNamespace(text="", candidates=[])

        async def aclose(self):
            pass

    monkeypatch.setattr(genai, "Client", EmptyClient)

    assert await MovieDataFallbackEngine.grounded_search("an utterly obscure question") is None


@pytest.mark.asyncio
async def test_grounded_search_uses_the_grounding_specific_timeout_setting(monkeypatch):
    """The HTTP timeout given to a Google-Search-grounded call must come
    from gemini_grounding_timeout_seconds, not the generic web_timeout_
    seconds (which is too tight for a grounded round trip)."""
    from google import genai
    from google.genai import types

    monkeypatch.setattr(settings, "gemini_api_key", "test-gemini")
    monkeypatch.setattr(settings, "gemini_grounding_timeout_seconds", 17.0)
    captured_http_options = {}

    class RecordingClient:
        def __init__(self, **kwargs):
            captured_http_options["timeout"] = kwargs["http_options"].timeout
            self.aio = SimpleNamespace(
                models=SimpleNamespace(generate_content=self.generate_content),
                aclose=self.aclose,
            )

        async def generate_content(self, **_kwargs):
            return SimpleNamespace(
                text="An answer.",
                candidates=[
                    SimpleNamespace(
                        grounding_metadata=SimpleNamespace(
                            grounding_chunks=[
                                SimpleNamespace(web=SimpleNamespace(uri="https://example.com/a"))
                            ]
                        )
                    )
                ],
            )

        async def aclose(self):
            pass

    monkeypatch.setattr(genai, "Client", RecordingClient)
    monkeypatch.setattr(types, "HttpOptions", types.HttpOptions)

    await MovieDataFallbackEngine.grounded_search("a question")

    assert captured_http_options["timeout"] == 17000


def test_collect_sources_flattens_and_ranks_evidence():
    from app.agent.tool_support import collect_sources

    evidence = {
        "movie": {
            "title": "Dune",
            "sources": ["https://www.themoviedb.org/movie/438631"],
        },
        "grounded_research": {
            "source_details": [
                {
                    "url": "https://r.example/1",
                    "title": "blog.example",
                    "source_domain": "blog.example",
                },
                {
                    "url": "https://r.example/2",
                    "title": "oscars.org",
                    "source_domain": "oscars.org",
                },
            ]
        },
    }
    sources = collect_sources(evidence)
    assert sources[0]["domain"] == "oscars.org" and sources[0]["tier"] == 3
    assert {item["kind"] for item in sources} == {"web", "structured"}


@pytest.mark.asyncio
async def test_runtime_recommendations_keep_strict_and_labelled_near_matches():
    class RecommendationTMDB:
        async def discover_movies(self, **kwargs):
            assert kwargs["runtime_max"] == 120
            assert "mood_keywords" not in kwargs  # moods rank, they never filter
            return {"results": [{"id": 1}, {"id": 2}, {"id": 3}]}

        async def get_movie_details(self, movie_id):
            return {
                "id": movie_id,
                "title": f"Film {movie_id}",
                "release_date": "2019-01-01",
                "runtime": {1: 120, 2: 135, 3: 136}[movie_id],
            }

        async def get_watch_providers(self, movie_id, region=None):
            return {
                "link": f"https://www.justwatch.com/us/movie/film-{movie_id}",
                "flatrate": [{"provider_name": "Netflix", "logo_path": "/netflix.png"}],
                "rent": [],
                "buy": [],
            }

    engine = MovieDataFallbackEngine(
        tmdb=RecommendationTMDB(),
        client=FakeHTTPClient([]),
    )
    intent = QueryClassification(
        domain="movie",
        intent="recommendation",
        needs_live_search=False,
        max_runtime_minutes=120,
    )

    result = await engine.lookup_query(intent, "recommend a movie under two hours", region="US")

    assert [movie["runtime"] for movie in result["strict_matches"]] == [120]
    assert [movie["runtime"] for movie in result["near_matches"]] == [135]
    assert result["near_matches"][0]["relaxed_constraint"]["over_by_minutes"] == 15
    assert result["strict_matches"][0]["watch_providers"]["flatrate"][0]["provider_name"] == "Netflix"


@pytest.mark.asyncio
async def test_runtime_lookup_returns_none_when_all_catalogs_are_empty(monkeypatch):
    class EmptyTMDB:
        async def search_movies(self, _title):
            return {"results": []}

    monkeypatch.setattr(settings, "omdb_api_key", None)
    monkeypatch.setattr(settings, "gemini_api_key", None)
    engine = MovieDataFallbackEngine(tmdb=EmptyTMDB(), client=FakeHTTPClient([]))
    intent = QueryClassification(
        domain="movie",
        intent="runtime",
        needs_live_search=False,
        movie_title="Unknown Film",
    )

    result = await engine.lookup_query(intent, "runtime of Unknown Film")

    assert result is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("constraints", "message"),
    [
        ({}, "recommend a movie"),
        (
            {"max_runtime_minutes": 90, "mood_keywords": ["dark", "coastal"]},
            "dark coastal movies under 90 minutes",
        ),
    ],
)
async def test_empty_recommendations_are_reported_explicitly(constraints, message):
    class EmptyRecommendationTMDB:
        async def discover_movies(self, **_kwargs):
            return {"results": []}

    engine = MovieDataFallbackEngine(
        tmdb=EmptyRecommendationTMDB(),
        client=FakeHTTPClient([]),
    )
    intent = QueryClassification(
        domain="movie",
        intent="recommendation",
        needs_live_search=False,
        **constraints,
    )

    result = await engine.lookup_query(intent, message)

    # An empty result is evidence ("nothing verified fits"), not a missing answer.
    assert result["status"] == "no_verified_candidates"
    assert result["strict_matches"] == [] and result["near_matches"] == []
