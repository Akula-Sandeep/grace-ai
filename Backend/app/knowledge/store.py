import hashlib
import json
from abc import ABC, abstractmethod

from .models import MovieKnowledge


class MovieKnowledgeStore(ABC):

    @abstractmethod
    async def get_by_id(
        self,
        movie_id: int,
    ) -> MovieKnowledge | None:
        raise NotImplementedError

    @abstractmethod
    async def get_by_title(
        self,
        title: str,
    ) -> MovieKnowledge | None:
        raise NotImplementedError

    @abstractmethod
    async def save(
        self,
        movie: MovieKnowledge,
    ) -> None:
        raise NotImplementedError

    @staticmethod
    def content_hash(content: str) -> str:
        return hashlib.sha256(content.strip().encode("utf-8")).hexdigest()


class InMemoryMovieKnowledgeStore(MovieKnowledgeStore):
    def __init__(self):
        self._by_id: dict[
            int,
            MovieKnowledge,
        ] = {}

        self._title_to_id: dict[
            str,
            int,
        ] = {}

    async def get_by_id(
        self,
        movie_id: int,
    ) -> MovieKnowledge | None:
        return self._by_id.get(movie_id)

    async def get_by_title(
        self,
        title: str,
    ) -> MovieKnowledge | None:
        movie_id = self._title_to_id.get(self._normalize_title(title))

        if movie_id is None:
            return None

        return self._by_id.get(movie_id)

    async def save(
        self,
        movie: MovieKnowledge,
    ) -> None:
        if movie.content_hash is None:
            canonical = json.dumps(
                movie.model_dump(exclude={"content_hash", "acquired_at"}),
                sort_keys=True,
                ensure_ascii=False,
                default=str,
            )
            movie = movie.model_copy(update={"content_hash": self.content_hash(canonical)})
        existing = self._by_id.get(movie.movie_id)
        if existing is not None and existing.content_hash == movie.content_hash:
            return
        self._by_id[movie.movie_id] = movie

        self._title_to_id[self._normalize_title(movie.title)] = movie.movie_id

        if movie.original_title:
            self._title_to_id[self._normalize_title(movie.original_title)] = movie.movie_id

    @staticmethod
    def _normalize_title(
        title: str,
    ) -> str:
        return " ".join(title.strip().lower().split())
