import pytest

from app.services.movie_intelligence_client import location_records
from app.services.movie_intelligence_page import build_category

ROWS = [
    {
        "zone": "filming_location",
        "place": "http://www.wikidata.org/entity/Q1",
        "placeLabel": "Wadi Rum",
        "coord": "Point(35.42 29.57)",
        "countryLabel": "Jordan",
        "partLabel": "Arrakis desert sequences",
        "refURL": "https://example.org/production-notes",
        "fictional": "false",
    },
    {
        "zone": "filming_location",
        "place": "http://www.wikidata.org/entity/Q1",
        "placeLabel": "Wadi Rum",
        "coord": "Point(35.42 29.57)",
        "countryLabel": "Jordan",
        "partLabel": "Sietch exteriors",
        "fictional": "false",
    },
    {
        "zone": "in_universe",
        "place": "http://www.wikidata.org/entity/Q2",
        "placeLabel": "Arrakis",
        "fictional": "true",
    },
    {"zone": "in_universe", "placeLabel": "Q123456", "fictional": "false"},
]


def test_rows_merge_into_one_record_per_place_with_details():
    records = location_records(ROWS)
    assert len(records) == 2
    wadi = next(r for r in records if r["place"] == "Wadi Rum")
    assert wadi["scenes"] == ["Arrakis desert sequences", "Sietch exteriors"]
    assert wadi["coordinates"] == {"lon": 35.42, "lat": 29.57}
    assert wadi["references"] == ["https://example.org/production-notes"]
    arrakis = next(r for r in records if r["place"] == "Arrakis")
    assert arrakis["fictional"] is True and arrakis["zone"] == "in_universe"


class Client:
    async def collect(self, category, title, year):
        return {
            "records": location_records(ROWS),
            "sources": [
                {"url": "https://a.com/x", "title": "A", "text": "The desert was filmed in Wadi Rum."},
                {"url": "https://b.com/y", "title": "B", "text": "Most scenes were shot in Abu Dhabi."},
            ],
        }


@pytest.mark.asyncio
async def test_locations_payload_has_details_map_links_and_conflicts():
    payload = await build_category(
        "locations", {"id": 1, "title": "Dune", "release_date": "2021-09-15"}, client=Client()
    )
    assert payload["story_locations"] == ["Arrakis"] and payload["filming_locations"] == ["Wadi Rum"]
    details = {d["place"]: d for d in payload["location_details"]}
    assert details["Arrakis"]["kind"] == "fictional" and details["Arrakis"]["map_url"] is None
    assert details["Wadi Rum"]["map_url"].startswith("https://www.openstreetmap.org/?mlat=29.57&mlon=35.42")
    assert details["Wadi Rum"]["scenes"] == ["Arrakis desert sequences", "Sietch exteriors"]
    assert payload["location_conflicts"][0]["topic"] == "filming location"
