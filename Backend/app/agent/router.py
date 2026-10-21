"""The validated request the tools and the action pipeline work from."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class QueryClassification(BaseModel):
    """A validated request for the tools and the action pipeline.

    Built from the model's tool-call arguments (and from a confirmed proposal), never
    by parsing the user's message.
    """

    model_config = ConfigDict(extra="forbid", strict=True)

    domain: Literal["movie", "general"]
    intent: Literal[
        "runtime",
        "songs",
        "music_soundtrack",
        "visual_analysis",
        "movie_awards",
        "movie_locations",
        "movie_facts",
        "cast_crew",
        "metadata",
        "discover",
        "compare",
        "recommendation",
        "general_fact",
        "coding_math",
        "casual_chat",
        "add_to_watchlist",
        "remove_from_watchlist",
        "mark_as_watched",
        "view_watchlist",
        "set_reminder",
        "rate_movie",
        "unmark_watched",
        # A reaction to a film ("loved it, but the second half dragged") and a
        # durable taste statement ("slow pacing kills movies for me").
        "movie_feedback",
        "preference_update",
    ]
    needs_live_search: bool
    movie_title: str | None = Field(default=None, min_length=1, max_length=200)
    person_name: str | None = Field(default=None, min_length=1, max_length=160)
    # Every film named in a comparison, in order ("Arrival vs Interstellar vs Contact").
    movie_titles: list[str] | None = Field(default=None, max_length=6)
    search_query: str | None = Field(default=None, min_length=1, max_length=500)
    max_runtime_minutes: int | None = Field(default=None, gt=0, le=1000)
    mood_keywords: list[str] | None = Field(default=None, max_length=8)
    year: int | None = Field(default=None, ge=1888, le=2200)
    # --- Constraint model (Jarvis spec §4): hard constraints, vetoes, soft
    # preferences and approximations are kept distinct so that nothing soft is
    # ever enforced as a filter and nothing hard is ever silently relaxed.
    min_runtime_minutes: int | None = Field(default=None, gt=0, le=1000)
    runtime_is_approximate: bool | None = None
    min_year: int | None = Field(default=None, ge=1888, le=2200)
    max_year: int | None = Field(default=None, ge=1888, le=2200)
    genres: list[str] | None = Field(default=None, max_length=6)
    exclude_genres: list[str] | None = Field(default=None, max_length=6)
    original_language: str | None = Field(default=None, pattern=r"^[a-z]{2}$")
    reference_titles: list[str] | None = Field(default=None, max_length=3)
    avoid_keywords: list[str] | None = Field(default=None, max_length=8)
    prefer_less_popular: bool | None = None
    # Ordered most-important first when the user ranks requirements (Jarvis spec §4.6).
    priorities: list[Literal["quality", "similarity", "mood", "runtime", "novelty"]] | None = Field(
        default=None, max_length=5
    )
    include_watched: bool | None = None
    # Hard constraint: only films based on true stories / real events.
    based_on_true_story: bool | None = None
    # Vetoes (Jarvis spec §4.4): qualities that eliminate a film outright
    # ("I absolutely don't want a depressing movie").
    veto_keywords: list[str] | None = Field(default=None, max_length=6)
    requested_fact: Literal["director", "runtime", "release", "composer"] | None = None
    # Actions: the time phrase for a reminder ("tomorrow at 8pm") and a 0-10 rating.
    reminder_when: str | None = Field(default=None, min_length=1, max_length=120)
    rating: float | None = Field(default=None, ge=0, le=10)
    # Filled from the user's saved preferences, never by the router model.
    preferred_genres: list[str] | None = Field(default=None, max_length=6)
    saved_preference_vetoes: list[str] | None = Field(default=None, max_length=6)

    @model_validator(mode="after")
    def validate_domain_and_live_search(self) -> QueryClassification:
        movie_intents = {
            "runtime",
            "songs",
            "music_soundtrack",
            "visual_analysis",
            "movie_awards",
            "movie_locations",
            "movie_facts",
            "cast_crew",
            "metadata",
            "discover",
            "compare",
            "recommendation",
            "add_to_watchlist",
            "remove_from_watchlist",
            "mark_as_watched",
            "view_watchlist",
            "set_reminder",
            "rate_movie",
            "unmark_watched",
            "movie_feedback",
            "preference_update",
        }
        if self.intent in movie_intents and self.domain != "movie":
            raise ValueError("Movie intents require domain='movie'.")
        if self.intent in {"coding_math", "casual_chat"} and self.domain != "general":
            raise ValueError("General intents require domain='general'.")
        if self.needs_live_search and not self.search_query:
            raise ValueError("Live queries require an optimized search_query.")
        if not self.needs_live_search and self.search_query is not None:
            raise ValueError("search_query is only valid for live-search routes.")
        return self
