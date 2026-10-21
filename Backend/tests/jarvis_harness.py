"""A conversation simulator for the Jarvis behaviour spec.

Drives the real signed-in chat endpoint (``main.chat_stream`` -> ``MovieAgent``
-> router -> dispatcher -> services -> database) turn by turn. Only the outside
world is faked: TMDB is an in-memory catalogue that behaves like the real API
(fuzzy, popularity-ordered search; list results without runtime), and the
language models either fail (``offline=True``, the owner's real situation) or
answer with a fixed sentence.
"""

from __future__ import annotations

import json
import os
import re
import uuid
from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.requests import Request


def film(
    mid,
    title,
    runtime,
    *,
    year,
    genres,
    votes,
    popularity,
    rating=7.6,
    overview="",
    keywords=(),
    director=None,
    composer=None,
    cast=(),
):
    crew = []
    if director:
        crew.append({"id": 9000 + mid, "name": director, "job": "Director", "department": "Directing"})
    if composer:
        crew.append(
            {"id": 8000 + mid, "name": composer, "job": "Original Music Composer", "department": "Sound"}
        )
    return {
        "id": mid,
        "title": title,
        "original_title": title,
        "runtime": runtime,
        "genre_ids": list(genres),
        "genres": [{"id": g} for g in genres],
        "release_date": f"{year}-06-01",
        "vote_count": votes,
        "popularity": popularity,
        "vote_average": rating,
        "original_language": "en",
        "overview": overview,
        "keywords": {"keywords": [{"id": i, "name": k} for i, k in enumerate(keywords)]},
        "credits": {
            "cast": [{"id": 7000 + mid * 10 + i, "name": n, "character": c} for i, (n, c) in enumerate(cast)],
            "crew": crew,
        },
    }


CATALOG = [
    film(
        155,
        "The Dark Knight",
        152,
        year=2008,
        genres=(18, 28, 80),
        votes=33000,
        popularity=120,
        rating=8.5,
        director="Christopher Nolan",
        keywords=("dc comics", "crime", "vigilante"),
    ),
    film(90001, "The Dark Knight", 25, year=1998, genres=(99,), votes=3, popularity=0.4, rating=6.0),
    film(268, "Batman", 126, year=1989, genres=(14, 28), votes=8000, popularity=40, director="Tim Burton"),
    film(
        157336,
        "Interstellar",
        169,
        year=2014,
        genres=(12, 18, 878),
        votes=36000,
        popularity=150,
        rating=8.4,
        director="Christopher Nolan",
        composer="Hans Zimmer",
        keywords=("space", "wormhole", "time dilation"),
    ),
    film(
        329865,
        "Arrival",
        116,
        year=2016,
        genres=(18, 878),
        votes=18000,
        popularity=60,
        rating=7.6,
        director="Denis Villeneuve",
        composer="Jóhann Jóhannsson",
        overview="An emotional first-contact drama.",
        keywords=("alien", "linguistics", "grief"),
    ),
    film(
        438631,
        "Dune",
        155,
        year=2021,
        genres=(12, 878),
        votes=13000,
        popularity=90,
        rating=7.8,
        director="Denis Villeneuve",
        composer="Hans Zimmer",
        keywords=("desert", "based on novel"),
    ),
    film(
        693134,
        "Dune: Part Two",
        167,
        year=2024,
        genres=(12, 878),
        votes=7000,
        popularity=95,
        rating=8.2,
        director="Denis Villeneuve",
        composer="Hans Zimmer",
        keywords=("desert", "sequel"),
    ),
    film(
        807,
        "Se7en",
        127,
        year=1995,
        genres=(80, 9648, 53),
        votes=21000,
        popularity=60,
        rating=8.4,
        director="David Fincher",
        overview="Two detectives hunt a dark, methodical serial killer.",
        keywords=("serial killer", "dark", "rain", "neo-noir"),
    ),
    film(
        1949,
        "Zodiac",
        157,
        year=2007,
        genres=(80, 18, 9648),
        votes=11000,
        popularity=35,
        rating=7.5,
        director="David Fincher",
        keywords=("based on true story", "serial killer", "slow burn"),
    ),
    film(
        68734,
        "Argo",
        120,
        year=2012,
        genres=(18, 53),
        votes=9000,
        popularity=30,
        rating=7.3,
        director="Ben Affleck",
        keywords=("based on true story", "cia", "tense"),
    ),
    film(
        363676,
        "Sully",
        96,
        year=2016,
        genres=(18,),
        votes=7000,
        popularity=25,
        rating=7.1,
        director="Clint Eastwood",
        keywords=("based on true story", "biography", "pilot"),
    ),
    film(
        1422,
        "The Departed",
        151,
        year=2006,
        genres=(80, 18, 53),
        votes=15000,
        popularity=45,
        rating=8.2,
        director="Martin Scorsese",
        keywords=("undercover", "crime"),
    ),
    film(
        475557,
        "Joker",
        122,
        year=2019,
        genres=(80, 53, 18),
        votes=25000,
        popularity=70,
        rating=8.1,
        director="Todd Phillips",
        keywords=("dc comics", "dark", "psychological"),
    ),
    film(
        146233,
        "Prisoners",
        153,
        year=2013,
        genres=(18, 53, 80),
        votes=12000,
        popularity=40,
        rating=8.1,
        director="Denis Villeneuve",
        keywords=("dark", "kidnapping", "rain"),
    ),
    film(
        64690,
        "Drive",
        100,
        year=2011,
        genres=(18, 53, 80),
        votes=12000,
        popularity=35,
        rating=7.6,
        director="Nicolas Winding Refn",
        keywords=("neo-noir", "dark", "getaway driver"),
    ),
    film(
        4547,
        "Panic Room",
        112,
        year=2002,
        genres=(80, 53),
        votes=5000,
        popularity=20,
        rating=6.8,
        director="David Fincher",
        keywords=("dark", "home invasion"),
    ),
    film(
        398818,
        "Edge of Tomorrow",
        113,
        year=2014,
        genres=(28, 878),
        votes=13000,
        popularity=50,
        rating=7.6,
        director="Doug Liman",
        keywords=("time loop", "aliens", "fast paced"),
    ),
    film(
        264660,
        "Ex Machina",
        108,
        year=2015,
        genres=(18, 878),
        votes=13000,
        popularity=40,
        rating=7.6,
        director="Alex Garland",
        keywords=("artificial intelligence", "cerebral"),
    ),
    film(
        62,
        "Coherence",
        89,
        year=2013,
        genres=(878, 53),
        votes=1800,
        popularity=12,
        rating=7.3,
        director="James Ward Byrkit",
        keywords=("parallel worlds", "mind-bending", "dark"),
    ),
    film(
        37799,
        "The Social Network",
        120,
        year=2010,
        genres=(18,),
        votes=12000,
        popularity=30,
        rating=7.4,
        director="David Fincher",
        keywords=("based on true story", "biography", "fast paced"),
    ),
]
PEOPLE = {
    "christopher nolan": 525,
    "denis villeneuve": 137427,
    "david fincher": 7467,
    "hans zimmer": 947,
}


def _norm(text):
    return re.sub(r"[^a-z0-9 ]+", " ", (text or "").casefold()).split()


COLLECTIONS = {726871: [438631, 693134]}
for _movie in CATALOG:
    for _cid, _parts in COLLECTIONS.items():
        if _movie["id"] in _parts:
            _movie["belongs_to_collection"] = {"id": _cid, "name": "Dune Collection"}


class FakeTMDB:
    """In-memory TMDB with the real API's search semantics."""

    def __init__(self, catalog=None):
        self.catalog = {m["id"]: m for m in (catalog or CATALOG)}

    @staticmethod
    def _list_item(movie):
        return {k: v for k, v in movie.items() if k not in {"runtime", "credits", "keywords", "genres"}}

    def _search(self, query):
        words = [w for w in _norm(query) if w not in {"the", "a", "an"}]
        hits = [m for m in self.catalog.values() if words and all(w in _norm(m["title"]) for w in words)]
        hits.sort(key=lambda m: m["popularity"], reverse=True)
        return {"results": [self._list_item(m) for m in hits]}

    async def _get(self, path, params=None, **_kwargs):
        params = params or {}
        if path == "/search/movie":
            result = self._search(params.get("query", ""))
            if params.get("year"):
                result["results"] = [
                    m for m in result["results"] if m["release_date"].startswith(str(params["year"]))
                ]
            return result
        if path == "/search/person":
            key = " ".join(_norm(params.get("query", "")))
            pid = PEOPLE.get(key)
            return {
                "results": (
                    [
                        {
                            "id": pid,
                            "name": params.get("query", "").title(),
                            "known_for_department": "Directing",
                        }
                    ]
                    if pid
                    else []
                )
            }
        match = re.fullmatch(r"/person/(\d+)", path)
        if match:
            pid = int(match.group(1))
            name = next((n for n, i in PEOPLE.items() if i == pid), "unknown").title()
            directed = [
                {
                    "id": m["id"],
                    "title": m["title"],
                    "release_date": m["release_date"],
                    "job": "Director",
                    "vote_count": m["vote_count"],
                    "popularity": m["popularity"],
                }
                for m in self.catalog.values()
                if any(c["name"].casefold() == name.casefold() for c in m["credits"]["crew"])
            ]
            return {
                "id": pid,
                "name": name,
                "movie_credits": {"cast": [], "crew": directed},
                "tv_credits": {},
            }
        match = re.fullmatch(r"/collection/(\d+)", path)
        if match:
            parts = [self._list_item(self.catalog[i]) for i in COLLECTIONS.get(int(match.group(1)), [])]
            return {"id": int(match.group(1)), "parts": parts}
        match = re.fullmatch(r"/movie/(\d+)(?:/.*)?", path)
        if match:
            return await self.get_movie_details(int(match.group(1)))
        return {"results": []}

    async def search_movies(self, q, page=1):
        return self._search(q)

    async def discover_movies(self, **kwargs):
        movies = list(self.catalog.values())
        if kwargs.get("with_keywords"):
            movies = [
                m
                for m in movies
                if any(k["name"] in {"based on true story", "biography"} for k in m["keywords"]["keywords"])
            ]
        if kwargs.get("runtime_max"):
            movies = [m for m in movies if (m["runtime"] or 0) <= kwargs["runtime_max"] + 15]
        if kwargs.get("page", 1) > 1:
            return {"results": []}
        movies.sort(key=lambda m: m["popularity"], reverse=True)
        return {"results": [self._list_item(m) for m in movies]}

    async def get_recommendations(self, movie_id, page=1):
        return {"results": []}

    async def get_similar_movies(self, movie_id, page=1):
        return {"results": []}

    async def get_movie_details(self, movie_id, **_kwargs):
        return json.loads(json.dumps(self.catalog[int(movie_id)]))

    async def get_watch_providers(self, movie_id, region=None):
        return {"flatrate": [{"provider_name": "Example+"}], "region": region}

    async def close(self):
        return None


class Offline:
    """A model provider that is unreachable, like Groq on the owner's machine."""

    provider_name = "groq"
    last_usage: dict = {}

    def __init__(self, name="openai/gpt-oss-20b"):
        self.model_name = name
        self.requests = []

    async def generate(self, request):
        self.requests.append(request)
        raise RuntimeError("connection error")

    async def generate_structured(self, request, schema):
        self.requests.append(request)
        raise RuntimeError("connection error")

    async def stream(self, request):
        self.requests.append(request)
        raise RuntimeError("connection error")
        yield ""  # pragma: no cover


def _request():
    class ConnectedRequest(Request):
        async def is_disconnected(self):
            return False

    return ConnectedRequest(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/v1/chat/stream",
            "headers": [],
            "query_string": b"",
            "server": ("testserver", 80),
            "client": ("127.0.0.1", 1234),
            "scheme": "http",
        }
    )


async def _events(response):
    events = []
    async for chunk in response.body_iterator:
        content = chunk.decode() if isinstance(chunk, bytes) else chunk
        for block in content.strip().split("\n\n"):
            lines = [line for line in block.splitlines() if not line.startswith(":")]
            if not lines:
                continue
            event = next((line[7:] for line in lines if line.startswith("event: ")), None)
            data = next((line[6:] for line in lines if line.startswith("data: ")), None)
            if event and data:
                events.append({"event": event, **json.loads(data)})
    return events


class Conversation:
    """One signed-in conversation with Grace."""

    def __init__(self, sessions, user_id):
        self.sessions = sessions
        self.user_id = user_id
        self.conversation_id = None
        self.transcript = []

    async def say(self, text, **extra):
        from app import main

        fields = {"client_message_id": uuid.uuid4(), **extra}
        payload = main.ChatIn(message=text, conversation_id=self.conversation_id, **fields)
        events = await _events(await main.chat_stream(payload, SimpleNamespace(id=self.user_id), _request()))
        if events and events[0]["event"] == "started":
            self.conversation_id = events[0].get("conversation_id") or self.conversation_id
        answer = "".join(e.get("text", "") for e in events if e["event"] == "token")
        errors = [e for e in events if e["event"] == "error"]
        self.transcript.append((text, answer or f"<error {errors[-1] if errors else events[-1:]}>"))
        return SimpleNamespace(text=answer, events=events, error=errors[-1] if errors else None)

    def user_message_ids(self):
        """uuid of every user message in this conversation, oldest first."""
        from sqlalchemy import select

        from app.database import Message

        with self.sessions() as db:
            return [
                row.uuid_id
                for row in db.scalars(
                    select(Message)
                    .where(Message.conversation_id == self.conversation_id, Message.role == "user")
                    .order_by(Message.id)
                )
            ]

    def reminders(self):
        from sqlalchemy import select

        from app.database import Notification

        with self.sessions() as db:
            return [
                row.title
                for row in db.scalars(select(Notification).where(Notification.user_id == self.user_id))
            ]

    def watchlist(self):
        from app.movie_state import MovieStateService

        with self.sessions() as db:
            return [item["title"] for item in MovieStateService(db, None).list_watchlist(self.user_id)]

    def watched(self):
        from app.movie_state import MovieStateService

        with self.sessions() as db:
            return [item["title"] for item in MovieStateService(db, None).list_watched(self.user_id)]

    def dump(self):
        return "\n".join(f"USER: {q}\nGrace: {a}" for q, a in self.transcript)


def install(monkeypatch, *, offline=True):
    """Fake the outside world for one test; returns a session factory."""
    from app import main
    from app.agent import agent as agent_module
    from app.database import Base
    from app.services import movie_data_fallback

    url = os.getenv("POSTGRES_TEST_DATABASE_URL")
    engine = (
        create_engine(url, pool_pre_ping=True)
        if url
        else create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(main, "SessionLocal", factory)
    monkeypatch.setattr(agent_module, "SessionLocal", factory)

    async def allow(*_args, **_kwargs):
        return True

    monkeypatch.setattr(main.shared_state, "rate_limit", allow)
    tmdb = FakeTMDB()
    monkeypatch.setattr(movie_data_fallback, "TMDBClient", lambda *a, **k: tmdb)

    async def nothing(*_args, **_kwargs):
        return None

    monkeypatch.setattr(
        movie_data_fallback.MovieDataFallbackEngine,
        "grounded_search",
        classmethod(lambda cls, *a, **k: nothing()),
    )
    if offline:
        fast, deep = Offline("openai/gpt-oss-20b"), Offline("openai/gpt-oss-120b")
        monkeypatch.setattr(
            main.agent, "router", SimpleNamespace(fast=lambda: fast, deep=lambda: deep, vision=lambda: fast)
        )
    return factory, tmdb


def new_user(factory):
    from app.database import User

    with factory() as db:
        suffix = uuid.uuid4().hex[:10]
        user = User(
            username=f"jarvis-{suffix}",
            email=f"jarvis-{suffix}@example.test",
            email_verified=True,
            status="verified",
            password_hash="unused",
        )
        db.add(user)
        db.commit()
        return user.id
