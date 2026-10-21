"""Mock-based regression coverage for check_wikidata()'s reporting shape.

scripts/live_provider_smoke.py is explicitly not run in CI (it needs live
network egress and real credentials), so this does not call it for real --
it monkeypatches MovieIntelligenceClient's own title-resolution and SPARQL
methods and checks the SHAPE of what check_wikidata() reports (the new
resolve_qid/total timing fields, and the ok/partial/fully-failed status
logic for award_people), which is pure Python behavior independent of the
network. See app/services/wikidata_client.py and
app/services/movie_intelligence_client.py for the production code these
exercise through MovieIntelligenceClient's public surface.
"""

import pytest

from app.services.movie_intelligence_client import MovieIntelligenceClient
from scripts.live_provider_smoke import check_wikidata


@pytest.mark.asyncio
async def test_check_wikidata_reports_resolve_qid_and_total_timing(monkeypatch):
    """The new resolve_qid= and total= fields must actually appear in the
    returned status line -- this is what lets the owner see, from a single
    run's output, how much of the total was title resolution (serial, no
    retry) versus the concurrent SPARQL batch, instead of one opaque
    aggregate number."""

    async def fake_wikidata_id(self, _title, _year=None):
        return "Q123"

    async def fake_sparql(self, query, *, raise_on_failure=False):
        if "pq:P1686" not in query:
            return [{"result": {"value": "Won"}, "awardLabel": {"value": "Academy Award"}}]
        if "P166" in query:
            return [{"awardLabel": {"value": "Academy Award"}, "personLabel": {"value": "A. Producer"}}]
        return []

    monkeypatch.setattr(MovieIntelligenceClient, "_wikidata_id", fake_wikidata_id)
    monkeypatch.setattr("app.services.wikidata_client.WikidataClient.query_sparql", fake_sparql)
    monkeypatch.setattr(
        "app.services.movie_intelligence_client.location_records",
        lambda rows: [{"zone": "filming_location", "nameLabel": "Example"}],
    )

    status = await check_wikidata()

    assert "resolve_qid=" in status
    assert "total=" in status
    assert "Q123" in status


@pytest.mark.asyncio
async def test_check_wikidata_reports_partial_award_people_honestly(monkeypatch):
    """If one of the two award-people sub-queries fails while the other
    succeeds, the status line must say "partial", not "ok" -- a smoke
    check that can't tell the difference is exactly what the owner's
    instructions warn against ("ensure the smoke-test status accurately
    reflects whether required queries ultimately succeeded")."""
    from app.services.upstream_errors import UpstreamTimeout

    async def fake_wikidata_id(self, _title, _year=None):
        return "Q123"

    async def fake_sparql(self, query, *, raise_on_failure=False):
        if "pq:P1686" not in query:
            return [{"result": {"value": "Won"}, "awardLabel": {"value": "Academy Award"}}]
        if "P166" in query:
            raise UpstreamTimeout("simulated timeout")
        return [{"awardLabel": {"value": "Academy Award"}, "personLabel": {"value": "A. Nominee"}}]

    monkeypatch.setattr(MovieIntelligenceClient, "_wikidata_id", fake_wikidata_id)
    monkeypatch.setattr("app.services.wikidata_client.WikidataClient.query_sparql", fake_sparql)
    monkeypatch.setattr(
        "app.services.movie_intelligence_client.location_records",
        lambda rows: [{"zone": "filming_location", "nameLabel": "Example"}],
    )

    status = await check_wikidata()

    assert "partial (award_people_won failed)" in status
    assert "ok" not in status.split("award_people=")[1].split()[0]
