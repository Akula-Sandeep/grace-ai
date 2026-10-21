import httpx
import pytest

from app.services import musicbrainz_client as mb
from app.services.movie_intelligence_page import build_category

GROUPS = {
    "release-groups": [
        {
            "id": "rg-wrong",
            "title": "Interstellar Overdrive",
            "secondary-types": ["Live"],
            "first-release-date": "1967",
        },
        {
            "id": "rg-1",
            "title": "Interstellar: Original Motion Picture Soundtrack",
            "secondary-types": ["Soundtrack"],
            "first-release-date": "2014-11-17",
            "score": 100,
            "releases": [{"id": "rel-promo", "status": "Promotion"}, {"id": "rel-1", "status": "Official"}],
        },
    ]
}
RELEASE = {
    "id": "rel-1",
    "title": "Interstellar: Original Motion Picture Soundtrack",
    "date": "2014-11-17",
    "country": "XW",
    "text-representation": {"language": "eng"},
    "label-info": [{"label": {"name": "WaterTower Music"}}],
    "media": [
        {
            "tracks": [
                {
                    "title": "Cornfield Chase",
                    "length": 126000,
                    "artist-credit": [{"name": "Hans Zimmer"}],
                    "recording": {
                        "relations": [
                            {"type": "producer", "artist": {"name": "Hans Zimmer"}},
                            {
                                "type": "performance",
                                "work": {
                                    "relations": [{"type": "composer", "artist": {"name": "Hans Zimmer"}}]
                                },
                            },
                        ]
                    },
                }
            ]
        }
    ],
}


def _transport():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        assert request.headers["User-Agent"].startswith("Grace/")
        assert request.url.params["fmt"] == "json"
        if request.url.path == "/ws/2/release-group":
            return httpx.Response(200, json=GROUPS)
        assert request.url.path == "/ws/2/release/rel-1"  # the official release, not the promo
        assert "work-level-rels" in request.url.params["inc"]
        return httpx.Response(200, json=RELEASE)

    return httpx.MockTransport(handler), seen


@pytest.mark.asyncio
async def test_soundtrack_lookup_picks_the_film_soundtrack_and_parses_credits(monkeypatch):
    monkeypatch.setattr(mb, "_cache", {})
    monkeypatch.setattr(mb, "_last_request", 0.0)
    transport, seen = _transport()
    async with httpx.AsyncClient(transport=transport, headers={"User-Agent": mb.USER_AGENT}) as client:
        album = await mb.soundtrack("Interstellar", 2014, client=client)
    assert album["title"] == "Interstellar: Original Motion Picture Soundtrack"
    assert album["labels"] == ["WaterTower Music"] and album["language"] == "English"
    track = album["tracks"][0]
    assert track == {
        "title": "Cornfield Chase",
        "length_seconds": 126,
        "artists": ["Hans Zimmer"],
        "producers": ["Hans Zimmer"],
        "lyricists": [],
        "composers": ["Hans Zimmer"],
    }
    assert len(seen) == 2


def test_wrong_year_or_non_soundtrack_groups_are_rejected():
    assert mb.pick_release_group(GROUPS["release-groups"], "Interstellar", 1990) is None
    assert mb.pick_release_group(GROUPS["release-groups"][:1], "Interstellar", None) is None


@pytest.mark.asyncio
async def test_music_page_uses_the_album_when_no_catalogue_tracks_exist():
    async def lookup(*_args):
        return {}

    async def album_lookup(_title, _year):
        return {**mb.parse_release(RELEASE), "source": "MusicBrainz"}

    details = {
        "id": 157336,
        "title": "Interstellar",
        "release_date": "2014-11-05",
        "composers": [{"name": "Hans Zimmer"}],
    }
    payload = await build_category("music", details, soundtrack_lookup=lookup, album_lookup=album_lookup)
    assert payload["albums"][0]["labels"] == ["WaterTower Music"]
    assert payload["producers"] == ["Hans Zimmer"]
    assert payload["tracks_by_language"]["English"][0]["composers"] == ["Hans Zimmer"]
    assert payload["compositions"][0]["versions"][0]["producers"] == ["Hans Zimmer"]
    assert any(s["title"].startswith("MusicBrainz") for s in payload["sources"])
