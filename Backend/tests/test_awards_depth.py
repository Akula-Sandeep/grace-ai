import pytest

from app.services.movie_intelligence_client import award_records
from app.services.movie_intelligence_page import build_category

FILM_ROWS = [
    {
        "result": "Won",
        "awardLabel": "Academy Award for Best Visual Effects",
        "bodyLabel": "Academy of Motion Picture Arts and Sciences",
        "ceremonyLabel": "87th Academy Awards",
        "year": "2015",
    },
    {"result": "Nominated", "awardLabel": "Academy Award for Best Visual Effects", "year": "2015"},
    {
        "result": "Nominated",
        "awardLabel": "Academy Award for Best Original Score",
        "bodyLabel": "Academy of Motion Picture Arts and Sciences",
        "year": "2015",
    },
]
PEOPLE_ROWS = [
    {
        "result": "Nominated",
        "awardLabel": "Academy Award for Best Original Score",
        "personLabel": "Hans Zimmer",
        "year": "2015",
    },
    {
        "result": "Won",
        "awardLabel": "Academy Award for Best Visual Effects",
        "personLabel": "Paul Franklin",
        "year": "2015",
    },
    {
        "result": "Won",
        "awardLabel": "Academy Award for Best Visual Effects",
        "personLabel": "Paul Franklin",
        "year": "2015",
    },
]


def test_records_merge_people_and_drop_the_duplicate_nomination_of_a_win():
    records = award_records(FILM_ROWS, PEOPLE_ROWS, "Interstellar")
    by_award = {(r["award"], r["result"]): r for r in records}
    assert ("Academy Award for Best Visual Effects", "Nominated") not in by_award
    vfx = by_award[("Academy Award for Best Visual Effects", "Won")]
    assert vfx["recipients"] == ["Paul Franklin"] and vfx["ceremony"] == "87th Academy Awards"
    assert by_award[("Academy Award for Best Original Score", "Nominated")]["recipients"] == ["Hans Zimmer"]


class Client:
    async def collect(self, category, title, year):
        return {
            "records": award_records(FILM_ROWS, PEOPLE_ROWS, title),
            "sources": [
                {
                    "url": "https://www.oscars.org/oscars/ceremonies/2015",
                    "title": "87th Academy Awards",
                    "text": "Interstellar won Best Visual Effects.",
                },
                {
                    "url": "https://www.bafta.org/film/awards",
                    "title": "BAFTA",
                    "text": "Interstellar was shortlisted for the EE Rising Star Award in 2015.",
                },
                {
                    "url": "https://someblog.net/x",
                    "title": "Blog",
                    "text": "It was shortlisted for Best Sound Mixing.",
                },
            ],
        }


@pytest.mark.asyncio
async def test_official_sources_verify_records_and_add_only_official_shortlists():
    payload = await build_category(
        "awards", {"id": 157336, "title": "Interstellar", "release_date": "2014-11-05"}, client=Client()
    )
    rows = {(a["award"], a["result"]): a for a in payload["awards"]}
    vfx = rows[("Academy Award for Best Visual Effects", "Won")]
    assert vfx["fact_status"] == "verified" and vfx["official_source"]["url"].startswith(
        "https://www.oscars.org/"
    )
    assert rows[("Academy Award for Best Original Score", "Nominated")]["fact_status"] == "strongly_supported"
    shortlisted = [a for a in payload["awards"] if a["result"] == "Shortlisted"]
    assert [a["award"] for a in shortlisted] == ["EE Rising Star Award"]  # the blog's claim is ignored
    assert payload["shortlisted"] == 1 and payload["wins"] == 1 and payload["nominations"] == 1
