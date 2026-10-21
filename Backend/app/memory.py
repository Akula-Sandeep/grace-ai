from __future__ import annotations

import logging
import math
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from hashlib import sha256

from sqlalchemy import desc, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.database import (
    Conversation,
    Memory,
    Message,
    Movie,
    MovieReference,
    Preference,
    Rating,
    UserPersonalizationSetting,
    WatchedItem,
    WatchlistItem,
)
from app.embeddings import EmbeddingDimensionError
from app.observability import log_event

_STOP = {
    "the",
    "and",
    "for",
    "with",
    "that",
    "this",
    "have",
    "from",
    "about",
    "what",
    "which",
    "movie",
    "movies",
    "film",
    "films",
    "want",
    "give",
    "something",
    "like",
    "into",
    "your",
    "you",
    "are",
    "was",
    "were",
    "not",
    "but",
    "too",
    "very",
    "just",
    "than",
    "does",
    "did",
    "would",
    "could",
    "should",
    "my",
    "me",
    "i",
    "it",
    "its",
}


def terms(q):
    return [x for x in re.findall(r"[\w'-]+", q.casefold()) if len(x) > 2 and x not in _STOP]


def any_terms_query(q: str) -> str | None:
    """A Postgres ``to_tsquery`` string matching ANY significant word of ``q``.

    ``plainto_tsquery`` requires every word, stopwords included, so a natural
    question ("given what I told you earlier, which version should I watch?")
    matched nothing. Ranking (``ts_rank_cd``) still prefers rows with more of
    the words. Only word characters reach the query, so it cannot be malformed.
    """
    words: list[str] = []
    # Split like Postgres' "simple" parser does ("o'brien's" -> "o", "brien", "s").
    for word in re.findall(r"\w+", (q or "").casefold()):
        if len(word) > 2 and word not in _STOP and word not in words:
            words.append(word)
    return " | ".join(words[:16]) or None


def normalize_memory_content(content: str) -> str:
    """Return the canonical form used for durable memory deduplication."""
    return " ".join(content.casefold().split())


def memory_content_hash(content: str) -> str:
    return sha256(normalize_memory_content(content).encode("utf-8")).hexdigest()


# MovieReference.source of the cards shown under a reply (kept so a reloaded chat shows them).
ANSWER_CARD = "answer_card"


class MemoryService:
    def __init__(self, db: Session):
        self.db = db

    def _is_postgres(self) -> bool:
        return self.db.get_bind().dialect.name == "postgresql"

    @staticmethod
    def _supports_vector_search() -> bool:
        return callable(getattr(Memory.embedding, "cosine_distance", None))

    def conversation_branch_ids(self, conversation_id, current_message_id):
        current = self.db.get(Message, current_message_id)
        if current is None or current.conversation_id != conversation_id:
            return None
        from app.database import branch_lineage_ids

        # One bounded recursive query instead of one query per ancestor.
        return branch_lineage_ids(self.db, conversation_id, current.parent_id)

    def relevant_history(self, uid, q, limit=8, conversation_id=None, current_message_id=None):
        ts = terms(q)
        if not ts:
            return []
        query = select(Message).where(
            Message.user_id == uid,
            Message.is_deleted.is_(False),
            Message.status.in_(("complete", "stopped")),
            Message.conversation_id.in_(select(Conversation.id).where(Conversation.is_deleted.is_(False))),
        )
        if conversation_id is not None:
            query = query.where(Message.conversation_id == conversation_id)
        if current_message_id is not None:
            branch_ids = self.conversation_branch_ids(conversation_id, current_message_id)
            if branch_ids is not None:
                query = query.where(Message.id.in_(branch_ids))

        if self._is_postgres():
            any_terms = any_terms_query(q)
            if any_terms is None:
                return []
            ts_query = func.to_tsquery("simple", any_terms)
            search_vector = func.to_tsvector("simple", Message.content)
            ranked = query.where(search_vector.op("@@")(ts_query)).add_columns(
                func.ts_rank_cd(search_vector, ts_query).label("_rank")
            )
            candidate_limit = max(32, min(256, limit * 8))
            rows = self.db.execute(
                ranked.order_by(
                    desc("_rank"),
                    Message.created_at.desc(),
                    Message.id.desc(),
                ).limit(candidate_limit)
            ).all()
            return [message for message, _rank in rows[:limit]]

        rows = self.db.scalars(query.order_by(Message.created_at.desc(), Message.id.desc()).limit(1500)).all()
        scored = []
        for message in rows:
            text = message.content.casefold()
            hits = sum(1 for term in ts if term in text)
            if hits:
                scored.append((hits / len(ts), message.created_at, message))
        return [
            message
            for _, _, message in sorted(scored, key=lambda item: (item[0], item[1]), reverse=True)[:limit]
        ]

    def prompt_memories(self, uid, q, *, relevant=6, total=16, char_budget=1800) -> list[str]:
        """The memories a model-led turn is given: the most relevant, then the standing ones.

        Lexical search only finds facts that share words with the message (see
        app/evals/memory_recall.py). The answering model is multilingual and
        handles paraphrase, so a user's facts are sent as a bounded list and the
        model decides which apply ("my gran" -> "watches with their grandmother").
        Past the bound, the model is told how many were left out and searches them
        with ``recall_conversation`` using keywords it writes itself.
        """
        if not self.memory_enabled(uid):
            return []
        chosen: list[str] = []
        used = 0
        for row in [
            *self.memories(uid, q, relevant, touch=False),
            *self.memories(uid, None, total, touch=False),
        ]:
            content = (row.content or "").strip()
            if not content or content in chosen:
                continue
            content = content if len(content) <= 200 else content[:199].rstrip() + "…"
            if used + len(content) > char_budget or len(chosen) >= total:
                break
            chosen.append(content)
            used += len(content)
        return chosen

    def memory_count(self, uid) -> int:
        if not self.memory_enabled(uid):
            return 0
        return int(self.db.scalar(select(func.count(Memory.id)).where(Memory.user_id == uid)) or 0)

    def memories(self, uid, q=None, limit=8, allow_cold_embedding=False, touch=True):
        if not self.memory_enabled(uid):
            return []
        if not q:
            return self.db.scalars(
                select(Memory)
                .where(Memory.user_id == uid)
                .order_by(
                    Memory.importance.desc(),
                    Memory.last_used_at.desc().nullslast(),
                    Memory.created_at.desc(),
                )
                .limit(limit)
            ).all()

        ts = terms(q)
        qvec = self._embedding(q, load=False)
        if not ts and not qvec:
            return []

        candidate_limit = max(32, min(256, limit * 8))
        candidates: dict[int, tuple[Memory, float, float]] = {}

        if self._is_postgres():
            any_terms = any_terms_query(q) if ts else None
            if any_terms:
                ts_query = func.to_tsquery("simple", any_terms)
                search_vector = func.to_tsvector("simple", Memory.content)
                lexical_query = (
                    select(Memory, func.ts_rank_cd(search_vector, ts_query).label("_rank"))
                    .where(Memory.user_id == uid, search_vector.op("@@")(ts_query))
                    .order_by(desc("_rank"), Memory.created_at.desc(), Memory.id.desc())
                    .limit(candidate_limit)
                )
                for memory, rank in self.db.execute(lexical_query).all():
                    candidates[memory.id] = (memory, min(1.0, float(rank or 0)), 0.0)

            if qvec and self._supports_vector_search():
                vector_query = (
                    select(
                        Memory,
                        Memory.embedding.cosine_distance(qvec).label("_distance"),
                    )
                    .where(Memory.user_id == uid, Memory.embedding.is_not(None))
                    .order_by("_distance", Memory.id.asc())
                    .limit(candidate_limit)
                )
                for memory, distance in self.db.execute(vector_query).all():
                    lexical = candidates.get(memory.id, (memory, 0.0, 0.0))[1]
                    semantic = max(0.0, 1.0 - float(distance or 0))
                    candidates[memory.id] = (memory, lexical, semantic)
        else:
            rows = self.db.scalars(
                select(Memory)
                .where(Memory.user_id == uid)
                .order_by(
                    Memory.importance.desc(),
                    Memory.last_used_at.desc().nullslast(),
                    Memory.created_at.desc(),
                )
                .limit(500)
            ).all()
            for memory in rows:
                text = memory.content.casefold()
                hits = sum(1 for term in ts if term in text)
                lexical = hits / len(ts) if ts else 0.0
                semantic = self._cosine(qvec, memory.embedding) if qvec and memory.embedding else 0.0
                if lexical or semantic:
                    candidates[memory.id] = (memory, lexical, semantic)

        scored = [
            (
                0.55 * semantic + 0.35 * lexical + 0.10 * float(memory.importance or 0),
                memory,
            )
            for memory, lexical, semantic in candidates.values()
            if lexical or semantic
        ]

        if allow_cold_embedding and (not qvec or len(scored) < min(3, limit)) and ts:
            qvec = self._embedding(q, load=True)
            if qvec:
                if self._is_postgres() and self._supports_vector_search():
                    vector_query = (
                        select(
                            Memory,
                            Memory.embedding.cosine_distance(qvec).label("_distance"),
                        )
                        .where(Memory.user_id == uid, Memory.embedding.is_not(None))
                        .order_by("_distance", Memory.id.asc())
                        .limit(candidate_limit)
                    )
                    for memory, distance in self.db.execute(vector_query).all():
                        lexical = candidates.get(memory.id, (memory, 0.0, 0.0))[1]
                        semantic = max(0.0, 1.0 - float(distance or 0))
                        candidates[memory.id] = (memory, lexical, semantic)
                elif candidates:
                    for memory_id, (memory, lexical, _semantic) in list(candidates.items()):
                        candidates[memory_id] = (
                            memory,
                            lexical,
                            self._cosine(qvec, memory.embedding),
                        )
                else:
                    rows = self.db.scalars(
                        select(Memory)
                        .where(Memory.user_id == uid)
                        .order_by(Memory.created_at.desc())
                        .limit(500)
                    ).all()
                    for memory in rows:
                        text = memory.content.casefold()
                        hits = sum(1 for term in ts if term in text)
                        lexical = hits / len(ts) if ts else 0.0
                        semantic = self._cosine(qvec, memory.embedding) if memory.embedding else 0.0
                        if lexical or semantic:
                            candidates[memory.id] = (memory, lexical, semantic)

                scored = [
                    (
                        0.70 * semantic + 0.20 * lexical + 0.10 * float(memory.importance or 0),
                        memory,
                    )
                    for memory, lexical, semantic in candidates.values()
                    if lexical or semantic
                ]

        out = [
            memory
            for _, memory in sorted(scored, key=lambda item: (item[0], item[1].importance), reverse=True)[
                :limit
            ]
        ]
        if touch and out:
            now = datetime.now(UTC)
            for memory in out:
                memory.last_used_at = now
            self.db.commit()
        return out

    def save_movie_references(self, uid, conversation_id, message_id, references):
        seen = set()
        ordinal = 0
        for ref in references:
            if not isinstance(ref, Mapping):
                log_event(
                    "movie_reference_fallback",
                    level=logging.WARNING,
                    details={"reason": "invalid_reference_shape"},
                )
                continue
            movie_like = any(
                k in ref
                for k in (
                    "tmdb_id",
                    "release_date",
                    "overview",
                    "vote_average",
                    "poster_url",
                    "poster_path",
                    "backdrop_url",
                    "runtime",
                )
            )
            try:
                mid = int(ref.get("tmdb_id") or (ref.get("id") if movie_like else 0))
            except (TypeError, ValueError):
                log_event(
                    "movie_reference_fallback",
                    level=logging.WARNING,
                    details={"reason": "invalid_tmdb_id"},
                )
                continue
            title = str(ref.get("title") or "").strip()
            if not mid or not title or mid in seen:
                continue
            seen.add(mid)
            ordinal += 1
            self.db.add(
                MovieReference(
                    conversation_id=conversation_id,
                    message_id=message_id,
                    user_id=uid,
                    tmdb_id=mid,
                    title=title,
                    ordinal=ordinal,
                    source=str(ref.get("source") or "tool_result"),
                )
            )
        if seen:
            self.db.commit()

    def profile(self, uid, limits=None):
        limits = limits or {}
        preference_limit = limits.get("preferences", 60)
        list_limit = limits.get("lists", 50)
        memory_limit = limits.get("memories", 12)
        prefs = self.db.scalars(
            select(Preference)
            .where(Preference.user_id == uid)
            .order_by(Preference.strength.desc(), Preference.confidence.desc())
            .limit(preference_limit)
        ).all()
        wl = self.db.scalars(
            select(WatchlistItem)
            .where(WatchlistItem.user_id == uid)
            .order_by(WatchlistItem.created_at.desc())
            .limit(list_limit)
        ).all()
        wd = self.db.scalars(
            select(WatchedItem)
            .where(WatchedItem.user_id == uid)
            .order_by(WatchedItem.watched_at.desc())
            .limit(list_limit)
        ).all()
        rt = self.db.scalars(
            select(Rating).where(Rating.user_id == uid).order_by(Rating.updated_at.desc()).limit(list_limit)
        ).all()
        all_watchlist_ids = set(
            self.db.scalars(select(WatchlistItem.movie_id).where(WatchlistItem.user_id == uid)).all()
        )
        all_watched_ids = set(
            self.db.scalars(select(WatchedItem.movie_id).where(WatchedItem.user_id == uid)).all()
        )
        movie_ids = {x.movie_id for x in [*wl, *wd, *rt]}
        movies = (
            {m.id: m for m in self.db.scalars(select(Movie).where(Movie.id.in_(movie_ids))).all()}
            if movie_ids
            else {}
        )

        def ref(mid):
            m = movies.get(mid)
            metadata = (m.metadata_json or {}) if m else {}
            genres = metadata.get("genres") or []
            genre_ids = metadata.get("genre_ids") or [
                genre.get("id") for genre in genres if isinstance(genre, dict) and genre.get("id")
            ]
            release_date = metadata.get("release_date") or metadata.get("releaseDate")
            release_year = str(release_date)[:4] if release_date else None
            return {
                "internal_id": mid,
                **(
                    {
                        "tmdb_id": m.tmdb_id,
                        "title": m.title,
                        "genre_ids": genre_ids,
                        "release_year": release_year,
                    }
                    if m
                    else {}
                ),
            }

        mems = (
            self.db.scalars(
                select(Memory)
                .where(Memory.user_id == uid)
                .order_by(Memory.importance.desc(), Memory.created_at.desc())
                .limit(memory_limit)
            ).all()
            if self.memory_enabled(uid)
            else []
        )
        return {
            "preferences": [
                {
                    "dimension": p.dimension,
                    "value": p.value,
                    "polarity": p.polarity,
                    "strength": round(p.strength, 3),
                    "confidence": round(p.confidence, 3),
                    "evidence_count": p.evidence_count,
                }
                for p in prefs
            ],
            "long_term_memories": [
                {
                    "type": m.memory_type,
                    "content": m.content,
                    "importance": round(m.importance, 3),
                }
                for m in mems
            ],
            "watchlist": [ref(x.movie_id) for x in wl],
            "watched": [ref(x.movie_id) for x in wd],
            "ratings": [{"movie": ref(x.movie_id), "rating": x.rating} for x in rt],
            "all_watchlist_tmdb_ids": (
                [
                    m.tmdb_id
                    for m in self.db.scalars(select(Movie).where(Movie.id.in_(all_watchlist_ids))).all()
                ]
                if all_watchlist_ids
                else []
            ),
            "all_watched_tmdb_ids": (
                [m.tmdb_id for m in self.db.scalars(select(Movie).where(Movie.id.in_(all_watched_ids))).all()]
                if all_watched_ids
                else []
            ),
        }

    def _embedding(self, text, load=False):
        try:
            from app.embeddings import EmbeddingService

            # Never cold-start a transformer model during ordinary chat retrieval.
            if not load and EmbeddingService._model is None:
                return None
            return EmbeddingService.encode(text)
        except ImportError:
            log_event(
                "memory_embedding_fallback",
                level=logging.WARNING,
                details={"reason": "optional_dependency_missing"},
            )
            return None

    @staticmethod
    def _cosine(a, b):
        if not a or not b:
            return 0.0
        if len(a) != len(b):
            raise EmbeddingDimensionError(
                f"Stored embedding dimension mismatch: expected {len(a)}, received {len(b)}."
            )
        den = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(x * x for x in b))
        return sum(x * y for x, y in zip(a, b, strict=True)) / den if den else 0.0

    def save_memory(
        self,
        uid,
        content,
        memory_type="episodic",
        importance=0.5,
        source_message_id=None,
    ):
        if not self.memory_enabled(uid):
            return None
        content = content.strip()
        if not content:
            return None
        content_hash = memory_content_hash(content)
        existing = self.db.scalar(
            select(Memory).where(
                Memory.user_id == uid,
                Memory.content_hash == content_hash,
            )
        )
        if existing:
            existing.importance = max(existing.importance, float(importance))
            self.db.commit()
            return existing

        m = Memory(
            user_id=uid,
            content=content,
            content_hash=content_hash,
            memory_type=memory_type,
            importance=max(0, min(1, float(importance))),
            source_message_id=source_message_id,
        )
        # Semantic memory is opportunistic: if the embedding model is already warm,
        # persist the vector; ordinary chat must never pay a transformer cold-start cost.
        vec = self._embedding(content, load=False)
        if vec:
            m.embedding = vec
        self.db.add(m)
        try:
            self.db.commit()
        except IntegrityError:
            self.db.rollback()
            existing = self.db.scalar(
                select(Memory).where(
                    Memory.user_id == uid,
                    Memory.content_hash == content_hash,
                )
            )
            if existing is None:
                raise
            existing.importance = max(existing.importance, float(importance))
            self.db.commit()
            return existing
        self.db.refresh(m)
        return m

    def memory_enabled(self, uid):
        setting = self.db.scalar(
            select(UserPersonalizationSetting).where(UserPersonalizationSetting.user_id == uid)
        )
        return setting is None or setting.memory_enabled

    def upsert_preference(
        self,
        uid,
        dimension,
        value,
        polarity="prefer",
        strength=0.5,
        confidence=0.5,
        source="inferred",
    ):
        p = self.db.scalar(
            select(Preference).where(
                Preference.user_id == uid,
                Preference.dimension == dimension,
                Preference.value == value,
                Preference.polarity == polarity,
            )
        )
        if p:
            # Evidence-weighted running update rather than overwriting history.
            p.strength = max(
                0,
                min(
                    1,
                    (p.strength * p.evidence_count + max(0, min(1, strength))) / (p.evidence_count + 1),
                ),
            )
            p.confidence = max(p.confidence, confidence)
            p.evidence_count += 1
            p.source = source
        else:
            p = Preference(
                user_id=uid,
                dimension=dimension,
                value=value,
                polarity=polarity,
                strength=max(0, min(1, strength)),
                confidence=max(0, min(1, confidence)),
                source=source,
            )
            self.db.add(p)
        self.db.commit()
        return p

    def replace_contradicting_preference(self, uid, dimension, value, polarity):
        """Drop an explicit preference that a newer explicit statement contradicts.

        "I love horror" followed later by "I can't stand horror" must leave one
        current preference, not two opposite ones of equal standing. Inferred
        preferences are left alone; explicit statements simply outrank them.
        """
        opposite = "avoid" if polarity == "prefer" else "prefer"
        stale = self.db.scalar(
            select(Preference).where(
                Preference.user_id == uid,
                Preference.dimension == dimension,
                Preference.value == value,
                Preference.polarity == opposite,
                Preference.source == "explicit",
            )
        )
        if stale is not None:
            self.db.delete(stale)
            self.db.flush()
        return stale is not None

    def infer_from_feedback(self, uid, feedback_text, movie_title=None, source_message_id=None):
        """Durable facts a model reads from the user's written feedback, saved as memories.

        No keyword rules: a small model extracts what the user stated
        (``app/services/memory_extraction.py``). Raises when no model could read
        it, so the background job retries later instead of guessing.
        """
        text = (feedback_text or "").strip()
        if not text or not self.memory_enabled(uid):
            return []
        import asyncio

        from app.inference.router import InferenceRouter
        from app.services.memory_extraction import extract_facts, save_facts

        message = f"My feedback on the film {movie_title}: {text}" if movie_title else text
        facts = asyncio.run(extract_facts(InferenceRouter().summary(), message))
        if facts is None:
            raise RuntimeError("feedback_model_unavailable")
        return save_facts(self, uid, facts, source_message_id)
