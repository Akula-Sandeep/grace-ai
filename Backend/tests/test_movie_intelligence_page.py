"""Movie page intelligence categories (product spec §8-§12, UI spec §52-§60)."""

import pytest

from app.services.movie_intelligence_page import build_category

DETAILS = {
    "id": 42,
    "title": "Example Film",
    "release_date": "2019-05-01",
    "spoken_languages": ["Telugu", "Hindi"],
    "music_directors": [{"name": "M. Composer"}],
    "songwriters": [{"name": "L. Writer"}],
}


class FakeClient:
    def __init__(self, data):
        self.data = data
        self.calls = []

    async def collect(self, category, title, year=None):
        self.calls.append((category, title, year))
        return self.data

    async def close(self):
        pass


@pytest.mark.asyncio
async def test_awards_separate_wins_from_nominations_and_pass_the_year():
    client = FakeClient(
        {
            "records": [
                {
                    "result": "Nominated",
                    "year": "2020",
                    "ceremony": "BAFTA",
                    "category": "Best Film",
                    "source": "Wikidata P1411",
                },
                {
                    "result": "Won",
                    "year": "2020",
                    "ceremony": "Academy Awards",
                    "category": "Best Song",
                    "source": "Wikidata P166",
                },
            ],
            "sources": [
                {
                    "title": "Variety",
                    "url": "https://variety.com/x",
                    "text": "won an Oscar",
                }
            ],
        }
    )
    payload = await build_category("awards", DETAILS, client=client)
    assert client.calls == [("awards", "Example Film", 2019)]
    assert [a["result"] for a in payload["awards"]] == ["Won", "Nominated"]
    assert (payload["wins"], payload["nominations"]) == (1, 1)
    assert payload["sources"][0]["authority"] == "strong"
    assert "incomplete" in payload["coverage"]


@pytest.mark.asyncio
async def test_locations_never_merge_story_and_filming():
    client = FakeClient(
        {
            "records": [
                {"zone": "in_universe", "place": "Gotham City"},
                {"zone": "filming_location", "place": "Chicago"},
            ],
            "sources": [],
        }
    )
    payload = await build_category("locations", DETAILS, client=client)
    assert payload["story_locations"] == ["Gotham City"]
    assert payload["filming_locations"] == ["Chicago"]


@pytest.mark.asyncio
async def test_facts_are_reported_claims_tied_to_sources():
    client = FakeClient(
        {
            "records": [],
            "sources": [
                {
                    "title": "Interview",
                    "url": "https://example.org/a",
                    "text": "Shot on 65mm film.",
                }
            ],
        }
    )
    payload = await build_category("facts", DETAILS, client=client)
    assert payload["facts"][0]["status"] == "reported"
    assert payload["facts"][0]["source"]["url"] == "https://example.org/a"


@pytest.mark.asyncio
async def test_empty_evidence_is_reported_as_no_verified_records():
    payload = await build_category("awards", DETAILS, client=FakeClient({"records": [], "sources": []}))
    assert payload["status"] == "no_verified_records"
    assert payload["awards"] == []


@pytest.mark.asyncio
async def test_music_groups_tracks_by_language_and_never_claims_completeness():
    seen = {}

    async def lookup(title, composer, languages):
        seen["args"] = (title, composer, languages)
        return {"Telugu": [{"track_name": "Song A", "singers": ["S1"]}], "Hindi": []}

    payload = await build_category("music", DETAILS, soundtrack_lookup=lookup, album_lookup=_no_album)
    assert seen["args"] == ("Example Film", "M. Composer", ("Telugu", "Hindi"))
    assert payload["composers"] == ["M. Composer"]
    assert payload["lyricists"] == ["L. Writer"]
    assert payload["tracks_by_language"] == {"Telugu": [{"title": "Song A", "singers": ["S1"]}]}
    assert payload["completeness"] == "partial"


@pytest.mark.asyncio
async def test_music_lookup_failure_degrades_to_credits():
    async def lookup(*_args):
        raise TimeoutError

    payload = await build_category("music", DETAILS, soundtrack_lookup=lookup, album_lookup=_no_album)
    assert payload["status"] == "found"
    assert payload["tracks_by_language"] == {}
    assert payload["completeness"] == "unknown"
    assert payload["lookup_error"] == "TimeoutError"


async def _no_album(_title, _year):
    return None
