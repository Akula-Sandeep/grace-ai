import pytest

from app.agent.router import QueryClassification
from app.agent.tools import fetch_movie_music
from app.services import youtube_music_client
from app.services.youtube_music_client import YouTubeMusicClient


class FakeYouTubeMusic:
    def __init__(self):
        self.searches = []
        self.album_ids = []

    def search(self, query, *, filter):
        self.searches.append((query, filter))
        return [{"browseId": f"album-{query.split()[2]}"}]

    def get_album(self, browse_id):
        self.album_ids.append(browse_id)
        return {
            "tracks": [
                {
                    "title": "Opening | Song",
                    "artists": [{"name": "Singer One"}, {"name": "Singer Two"}],
                    "description": "Vocals: Playback Singer & Guest Singer",
                },
                {"title": "Instrumental", "artists": []},
                {"artists": [{"name": "Missing title singer"}]},
            ]
        }


@pytest.mark.asyncio
async def test_youtube_music_client_returns_tracks_grouped_by_language():
    api = FakeYouTubeMusic()
    client = YouTubeMusicClient(api)

    result = await client.get_official_soundtracks(
        "Example Film",
        "Composer Name",
        ["Telugu", "Hindi"],
    )

    assert api.searches == [
        ("Example Film Telugu Composer Name", "albums"),
        ("Example Film Hindi Composer Name", "albums"),
    ]
    assert api.album_ids == ["album-Telugu", "album-Hindi"]
    assert result["Telugu"] == [
        {
            "track_name": "Opening | Song",
            "singers": [
                "Singer One",
                "Singer Two",
                "Playback Singer",
                "Guest Singer",
            ],
        },
        {"track_name": "Instrumental", "singers": []},
    ]
    assert result["Hindi"] == result["Telugu"]
    assert (
        await client.get_official_soundtracks(
            "Example Film",
            "Composer Name",
            ["Telugu", "Hindi"],
        )
        == result
    )
    assert len(api.searches) == 2


def test_youtube_music_extracts_and_normalizes_singers():
    assert YouTubeMusicClient._extract_singers(
        "Film Theme | Singer: Asha Bhosle & Sonu Nigam",
        "Sung by: Asha Bhosle\nMusic by: Composer",
    ) == ["Asha Bhosle", "Sonu Nigam"]


@pytest.mark.asyncio
async def test_youtube_music_client_returns_empty_language_on_provider_error():
    class BrokenYouTubeMusic:
        def search(self, **_kwargs):
            raise RuntimeError("provider structure changed")

    result = await YouTubeMusicClient(BrokenYouTubeMusic()).get_official_soundtracks(
        "Example Film",
        "Composer Name",
        ["Telugu"],
    )

    assert result == {"Telugu": []}


@pytest.mark.asyncio
async def test_youtube_music_client_returns_empty_language_on_timeout(monkeypatch):
    class SlowYouTubeMusic:
        def search(self, **_kwargs):
            import time

            time.sleep(0.05)
            return []

    monkeypatch.setattr(youtube_music_client, "REQUEST_TIMEOUT_SECONDS", 0.001)
    result = await YouTubeMusicClient(SlowYouTubeMusic()).get_official_soundtracks(
        "Example Film",
        "Composer Name",
        ["Telugu"],
    )

    assert result == {"Telugu": []}


@pytest.mark.asyncio
async def test_fetch_movie_music_returns_the_credited_composer_and_tracks_as_data(monkeypatch):
    class FakeTMDB:
        async def search_movies(self, title):
            assert title == "Example Film"
            return {
                "results": [
                    {"id": 1, "title": "Another Film"},
                    {"id": 2, "title": "Example Film"},
                ]
            }

        async def get_movie_details(self, movie_id):
            assert movie_id == 2
            return {
                "credits": {
                    "crew": [
                        {"name": "Composer Name", "job": "Original Music Composer"},
                        {"name": "Not the composer", "job": "Producer"},
                    ]
                }
            }

    async def soundtracks(movie_title, music_director):
        assert (movie_title, music_director) == ("Example Film", "Composer Name")
        return {
            "Telugu": [
                {
                    "track_name": "Opening | Song",
                    "singers": ["Singer One", "Singer Two"],
                }
            ],
            "Hindi": [],
        }

    monkeypatch.setattr(
        "app.agent.tools.get_official_soundtracks",
        soundtracks,
    )

    result = await fetch_movie_music("Example Film", FakeTMDB())

    # Data for the model, which decides how to present it (no pre-built table).
    assert result == {
        "status": "found",
        "title": "Example Film",
        "composers": ["Composer Name"],
        "tracks": [
            {"language": "Telugu", "track": "Opening | Song", "singers": ["Singer One", "Singer Two"]}
        ],
        "note": None,
    }


def test_music_soundtrack_is_a_valid_movie_intent():
    classification = QueryClassification(
        domain="movie",
        intent="music_soundtrack",
        needs_live_search=False,
        movie_title="Example Film",
    )

    assert classification.intent == "music_soundtrack"
