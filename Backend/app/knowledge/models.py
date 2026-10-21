from datetime import UTC, datetime

from pydantic import BaseModel, Field


class MovieKnowledge(BaseModel):
    movie_id: int

    title: str
    original_title: str | None = None

    runtime: int | None = None
    release_date: str | None = None

    genres: list[str] = Field(default_factory=list)

    overview: str | None = None
    director: str | None = None
    tagline: str | None = None

    vote_average: float | None = None
    vote_count: int | None = None

    original_language: str | None = None
    origin_country: list[str] = Field(default_factory=list)

    source: str = "tmdb"
    content_hash: str | None = None

    acquired_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
