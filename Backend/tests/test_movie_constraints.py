import pytest

from app.services.tmdb_client import TMDBClient


@pytest.mark.asyncio
async def test_discover_movies_applies_fifteen_minute_buffer_and_keyword_ids(
    monkeypatch,
):
    client = TMDBClient()
    calls = []

    async def fake_get(path, params=None):
        calls.append((path, params))
        if path == "/search/keyword":
            matching = {
                "dark": [{"id": 11, "name": "Dark"}],
                "darkness": [],
                "coast": [{"id": 23, "name": "Coast"}],
                "coastal": [{"id": 24, "name": "Coastal town"}],
                "seaside": [],
                "ocean": [],
            }
            return {"results": matching[params["query"]]}
        return {"results": []}

    monkeypatch.setattr(client, "_get", fake_get)
    await client.discover_movies(
        runtime_max=120,
        mood_keywords=["dark", "coastal"],
    )

    discover_params = next(params for path, params in calls if path == "/discover/movie")
    assert discover_params["with_runtime.lte"] == 135
    # Moods are soft: any mapped mood keyword widens discovery (OR); an
    # AND across moods made most mood requests return nothing.
    assert discover_params["with_keywords"] == "11|23|24"


@pytest.mark.asyncio
async def test_discover_movies_tags_candidates_inside_runtime_buffer(monkeypatch):
    client = TMDBClient()

    async def fake_get(_path, _params=None):
        return {
            "results": [
                {"id": 1, "title": "Strict match", "runtime": 120},
                {"id": 2, "title": "Buffer match", "runtime": 127},
                {"id": 3, "title": "Beyond buffer", "runtime": 136},
            ]
        }

    monkeypatch.setattr(client, "_get", fake_get)
    result = await client.discover_movies(runtime_max=120)

    strict, buffered, beyond = result["results"]
    assert "is_overflow" not in strict
    assert buffered["is_overflow"] is True
    assert buffered["overflow_minutes"] == 7
    assert "is_overflow" not in beyond


@pytest.mark.asyncio
async def test_unresolved_mood_keywords_never_empty_discovery(
    monkeypatch,
):
    client = TMDBClient()
    requested_paths = []

    async def fake_get(path, _params=None):
        requested_paths.append(path)
        return {"results": []}

    monkeypatch.setattr(client, "_get", fake_get)
    result = await client.discover_movies(mood_keywords=["unmapped mood"])

    # A soft preference that cannot be mapped must not erase the results;
    # ranking (not filtering) is responsible for mood fit.
    assert result == {"results": []}
    assert "/discover/movie" in requested_paths


@pytest.mark.asyncio
async def test_get_watch_providers_returns_region_and_provider_categories(monkeypatch):
    client = TMDBClient()
    calls = []

    async def fake_get(path, params=None):
        calls.append((path, params))
        return {
            "results": {
                "US": {
                    "link": "https://www.justwatch.com/us/movie/example-film",
                    "flatrate": [
                        {
                            "provider_name": "Netflix",
                            "logo_path": "/netflix.png",
                            "provider_id": 8,
                        }
                    ],
                    "rent": [{"provider_name": "Prime Video", "logo_path": "/prime.png"}],
                    "buy": [{"provider_name": "Store", "logo_path": "/store.png"}],
                },
                "GB": {"link": "https://www.justwatch.com/gb/movie/example-film"},
            }
        }

    monkeypatch.setattr(client, "_get", fake_get)

    result = await client.get_watch_providers(123, region="us")

    assert calls == [("/movie/123/watch/providers", None)]
    assert result == {
        "link": "https://www.justwatch.com/us/movie/example-film",
        "flatrate": [{"provider_name": "Netflix", "logo_path": "/netflix.png"}],
        "rent": [{"provider_name": "Prime Video", "logo_path": "/prime.png"}],
        "buy": [{"provider_name": "Store", "logo_path": "/store.png"}],
    }


@pytest.mark.asyncio
async def test_get_watch_providers_uses_configured_default_region(monkeypatch):
    client = TMDBClient()
    calls = []

    async def fake_get(path, params=None):
        calls.append((path, params))
        return {"results": {"NL": {"link": "https://www.justwatch.com/nl/movie/example-film"}}}

    monkeypatch.setattr(client, "_get", fake_get)
    monkeypatch.setattr("app.services.tmdb_client.settings.default_region", "NL")

    result = await client.get_watch_providers(123)

    assert result["link"] == "https://www.justwatch.com/nl/movie/example-film"
    assert calls == [("/movie/123/watch/providers", None)]


@pytest.mark.asyncio
async def test_get_watch_providers_returns_empty_categories_for_unknown_region(
    monkeypatch,
):
    client = TMDBClient()

    async def fake_get(_path, _params=None):
        return {"results": {"US": {"link": "https://www.justwatch.com/us/movie/example-film"}}}

    monkeypatch.setattr(client, "_get", fake_get)

    assert await client.get_watch_providers(456, region="CA") == {
        "link": None,
        "flatrate": [],
        "rent": [],
        "buy": [],
    }
