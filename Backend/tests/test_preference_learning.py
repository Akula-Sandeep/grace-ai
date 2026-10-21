"""Explicit taste learning (Jarvis spec §13-§16) and its use in recommendations."""

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.agent.router import QueryClassification
from app.agent.tool_support import apply_saved_preferences
from app.database import Base, Memory, Preference, User, UserPersonalizationSetting
from app.memory import MemoryService
from app.services.recommendation_engine import recommend
from tests.test_recommendation_engine import FakeTMDB, movie


@pytest.fixture
def db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path}/prefs.db")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, expire_on_commit=False)()
    user = User(username="taste-user", password_hash="x")
    session.add(user)
    session.commit()
    session.info["uid"] = user.id
    yield session
    session.close()
    engine.dispose()


def _model_reads(monkeypatch, facts):
    seen = []

    async def extract(_model, message):
        seen.append(message)
        return facts

    monkeypatch.setattr("app.services.memory_extraction.extract_facts", extract)
    return seen


def test_written_feedback_is_read_by_a_model_and_saved_as_memories(db, monkeypatch):
    """No keyword rules: what the user stated is the model's reading of it."""
    uid = db.info["uid"]
    seen = _model_reads(
        monkeypatch,
        [{"text": "Finds slow pacing a dealbreaker.", "kind": "preference"}],
    )
    MemoryService(db).infer_from_feedback(uid, "the pacing killed it for me", movie_title="Solaris")
    assert seen == ["My feedback on the film Solaris: the pacing killed it for me"]
    assert [m.content for m in db.scalars(select(Memory).where(Memory.user_id == uid))] == [
        "Finds slow pacing a dealbreaker."
    ]
    assert db.scalars(select(Preference)).all() == []  # stated tastes are saved by remember_taste in chat


def test_feedback_no_model_could_read_is_retried_not_guessed(db, monkeypatch):
    _model_reads(monkeypatch, None)
    with pytest.raises(RuntimeError, match="feedback_model_unavailable"):
        MemoryService(db).infer_from_feedback(db.info["uid"], "I hate horror movies")
    assert db.scalars(select(Memory)).all() == []


def test_nothing_is_learned_when_memory_is_disabled(db, monkeypatch):
    uid = db.info["uid"]
    seen = _model_reads(monkeypatch, [{"text": "Avoids horror.", "kind": "preference"}])
    db.add(UserPersonalizationSetting(user_id=uid, memory_enabled=False))
    db.commit()
    MemoryService(db).infer_from_feedback(uid, "I hate horror movies")
    assert seen == [] and db.scalars(select(Memory)).all() == []


def base(**kwargs):
    return QueryClassification(domain="movie", intent="recommendation", needs_live_search=False, **kwargs)


SAVED = [
    {
        "dimension": "genre",
        "value": "Horror",
        "polarity": "avoid",
        "source": "explicit",
    },
    {
        "dimension": "genre",
        "value": "Science Fiction",
        "polarity": "prefer",
        "source": "explicit",
    },
    {"dimension": "trait", "value": "gore", "polarity": "avoid", "source": "explicit"},
    {
        "dimension": "genre",
        "value": "Comedy",
        "polarity": "avoid",
        "source": "inferred",
    },
]


def test_saved_veto_excludes_genre_and_inferred_dislike_does_not():
    applied = apply_saved_preferences(base(), SAVED)
    assert applied.exclude_genres == ["Horror"]
    assert applied.saved_preference_vetoes == ["Horror"]
    assert applied.preferred_genres == ["Science Fiction"]
    assert applied.avoid_keywords == ["gore"]


def test_current_request_overrides_saved_veto():
    applied = apply_saved_preferences(base(genres=["horror"]), SAVED)
    assert not applied.exclude_genres
    assert applied.saved_preference_vetoes is None


@pytest.mark.asyncio
async def test_recommendations_never_return_a_vetoed_genre_and_say_why():
    catalog = [
        movie(1, "Scary One", 100, genres=(27,), rating=9.0),
        movie(2, "Space One", 110, genres=(878,), rating=7.0),
        movie(3, "Drama One", 105, genres=(18,), rating=7.2),
    ]
    tmdb = FakeTMDB(catalog)
    result = await recommend(apply_saved_preferences(base(), SAVED), tmdb=tmdb)
    titles = [m["title"] for m in result["strict_matches"]]
    assert "Scary One" not in titles
    assert titles[0] == "Space One"  # liked genre ranks first even with a lower rating
    assert result["saved_preferences_applied"]["excluded_genres"] == ["Horror"]


def test_one_reaction_does_not_become_a_ranking_penalty():
    from app.services.preference_learning import personal_recommendation_signals

    once = [
        {
            "dimension": "aspect",
            "value": "slow pacing",
            "polarity": "avoid",
            "source": "inferred",
            "evidence_count": 1,
        }
    ]
    twice = [{**once[0], "evidence_count": 2}]
    assert personal_recommendation_signals(once)["avoid_terms"] == []
    assert personal_recommendation_signals(twice)["avoid_terms"] == ["slow pacing"]


def test_saved_taste_fills_moods_only_when_the_request_has_none():
    from app.agent.router import QueryClassification
    from app.agent.tool_support import apply_saved_preferences

    saved = [{"dimension": "trait", "value": "dark atmosphere", "polarity": "prefer", "source": "explicit"}]
    plain = QueryClassification(
        domain="movie",
        intent="recommendation",
        needs_live_search=False,
        reference_titles=["Se7en"],
    )
    assert apply_saved_preferences(plain, saved).mood_keywords == ["dark"]
    tonight = plain.model_copy(update={"mood_keywords": ["light", "funny"]})
    assert apply_saved_preferences(tonight, saved).mood_keywords == ["light", "funny"]
