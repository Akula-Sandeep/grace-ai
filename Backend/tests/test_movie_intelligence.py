import pytest

from app.services.movie_intelligence_client import MovieIntelligenceClient


class FakeWebSearch:
    def __init__(self, results=()):
        self.results = list(results)
        self.queries = []

    async def search(self, query):
        self.queries.append(query)
        return {"results": self.results}

    async def close(self):
        pass


class FakeWikidata:
    async def close(self):
        pass


@pytest.mark.asyncio
async def test_location_collection_queries_wikidata_p840_and_p915():
    client = MovieIntelligenceClient(web=FakeWebSearch(), wikidata=FakeWikidata())
    client._wikidata_id = lambda _title: None

    async def wikidata_id(_title):
        return "Q999"

    queries = []

    async def sparql(query):
        queries.append(query)
        return [
            {"zone": "in_universe", "placeLabel": "Gotham City", "fictional": "true"},
            {
                "zone": "filming_location",
                "placeLabel": "Chicago",
                "countryLabel": "United States",
                "fictional": "false",
            },
        ]

    client._wikidata_id = wikidata_id
    client._sparql = sparql

    result = await client.collect("locations", "Example Film")

    assert "p:P840" in queries[0] and "p:P915" in queries[0]
    assert [(r["zone"], r["place"], r["source"], r["fictional"]) for r in result["records"]] == [
        ("in_universe", "Gotham City", "Wikidata P840", True),
        ("filming_location", "Chicago", "Wikidata P915", False),
    ]
    assert result["records"][1]["country"] == "United States"


@pytest.mark.asyncio
async def test_facts_collection_discards_generic_fluff_and_uses_targeted_query():
    web = FakeWebSearch(
        [
            {
                "title": "Equipment",
                "url": "https://example.org/equipment",
                "snippet": "The film used IMAX cameras and 65mm film.",
            },
            {
                "title": "Cast chemistry",
                "url": "https://example.org/fluff",
                "snippet": "The cast had great chemistry throughout production.",
            },
        ]
    )
    client = MovieIntelligenceClient(web=web, wikidata=FakeWikidata())

    result = await client.collect("facts", "Example Film")

    assert web.queries == ['"Example Film" production "behind the scenes" trivia budget lenses camera']
    assert [item["title"] for item in result["sources"]] == ["Equipment"]


def test_award_people_queries_anchor_on_the_selective_qualifier_first():
    """Regression guard for the SPARQL performance fix: the qualifier triple
    (pq:P1686, "for this work") must appear before the unselective
    ?person p:P166/p:P1411 pattern, so the query engine narrows to this
    film's award statements before considering every person's award
    statements on Wikidata. A reordering here is exactly the kind of change
    that silently reintroduces the original timeout.

    The combined UNION query from Batch 11/13 was split (this batch) into
    two independent single-branch queries -- one per result -- because the
    UNION itself still timed out in practice even with correct anchoring:
    Blazegraph's planner has strictly more work to do reasoning about two
    branches together than about either alone, and splitting also means a
    timeout on one result no longer costs the other's already-arrived
    rows. Each split query must keep the anchoring fix and the human-only
    correctness filter; neither should regress a UNION back in."""
    from app.services.movie_intelligence_client import (
        _award_people_nominated_query,
        _award_people_won_query,
    )

    won_query = _award_people_won_query("Q123")
    nominated_query = _award_people_nominated_query("Q123")

    for query, main_property in ((won_query, "P166"), (nominated_query, "P1411")):
        assert "UNION" not in query
        assert "wdt:P31 wd:Q5" in query  # human-only correctness filter preserved
        qualifier_pos = query.index("pq:P1686")
        person_pos = query.index("?person p:")
        assert qualifier_pos < person_pos, (
            "the selective film-qualifier triple must be evaluated before "
            "the unselective ?person p: pattern"
        )
        assert main_property in query


@pytest.mark.asyncio
async def test_awards_collection_runs_all_three_award_queries_concurrently(monkeypatch):
    """collect("awards", ...) must issue the film-awards query and BOTH
    award-people queries (won, nominated) together (asyncio.gather), not
    one after another -- otherwise a slow query's latency simply stacks on
    top of the others' within the same overall request budget. This is
    three concurrent calls now, not two, since award_people was split."""
    import asyncio

    client = MovieIntelligenceClient(web=FakeWebSearch(), wikidata=FakeWikidata())

    async def wikidata_id(_title):
        return "Q123"

    in_flight = 0
    max_in_flight = 0
    lock = asyncio.Lock()

    async def sparql(query):
        nonlocal in_flight, max_in_flight
        async with lock:
            in_flight += 1
            max_in_flight = max(max_in_flight, in_flight)
        await asyncio.sleep(0.05)
        async with lock:
            in_flight -= 1
        return []

    client._wikidata_id = wikidata_id
    client._sparql = sparql

    await client.collect("awards", "Example Film")

    assert max_in_flight == 3


@pytest.mark.asyncio
async def test_awards_collection_keeps_partial_people_results_when_one_query_fails(monkeypatch):
    """If one of the two award-people queries fails or times out while the
    other succeeds, collect("awards", ...) must still return the people who
    DID resolve, attributed to the correct result -- not drop every
    award-person credit just because one half of the split failed.
    _sparql() already returns [] rather than raising on a failed/timed-out
    query (query_sparql's raise_on_failure defaults to False), so this
    guards that collect() actually treats a [] from one branch as a
    partial result, not as a signal to discard the other branch's rows."""
    client = MovieIntelligenceClient(web=FakeWebSearch(), wikidata=FakeWikidata())

    async def wikidata_id(_title):
        return "Q123"

    async def sparql(query):
        if "pq:P1686" not in query:
            return [
                {"result": "Won", "awardLabel": "Academy Award for Best Picture", "year": "2024"},
                {"result": "Nominated", "awardLabel": "BAFTA Award for Best Film", "year": "2024"},
            ]
        if "P166" in query:
            # Simulates a timed-out/failed query_sparql call: empty, not raised.
            return []
        return [{"awardLabel": "BAFTA Award for Best Film", "personLabel": "A. Nominee"}]

    client._wikidata_id = wikidata_id
    client._sparql = sparql

    result = await client.collect("awards", "Example Film")

    # The nominated-people query's row still shows up, correctly attributed
    # to the matching nomination, even though the won-people query came
    # back empty (as if it had timed out).
    assert result["records"] == [
        {
            "award": "Academy Award for Best Picture",
            "body": None,
            "ceremony": None,
            "year": "2024",
            "result": "Won",
            "recipients": [],
            "recipient": "Example Film",
            "source": "Wikidata P166 (award received)",
        },
        {
            "award": "BAFTA Award for Best Film",
            "body": None,
            "ceremony": None,
            "year": "2024",
            "result": "Nominated",
            "recipients": ["A. Nominee"],
            "recipient": "Example Film",
            "source": "Wikidata P1411 (nominated for)",
        },
    ]
