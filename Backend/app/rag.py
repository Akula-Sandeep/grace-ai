from __future__ import annotations

import hashlib
import logging
import re

from sqlalchemy import select

from app.database import KnowledgeDocument, Vector
from app.observability import log_event

# Question words and function words: matching them ranks a snippet by how much
# English it contains, not by what it says (app/evals/rag_relevance.py).
_STOPWORDS = frozenset(
    """a an the and or but of in on at to for from by with as is are was were be been being am do does
    did done have has had having it its this that these those there here what which who whom whose how
    why where when than then so such too very can could would should will shall may might must just
    also any all some more most much many other into over under about after before while during not
    no nor only own same out off up down again you your yours me my mine our ours they them their he
    him his she her hers we us i if because until both each few""".split()
)


def _stem(word: str) -> str:
    """A light suffix stripper (no dependency): filmed/filming -> film, reviews/reviewers -> review."""
    for suffix in ("ations", "ation", "ings", "ing", "ers", "er", "ies", "ied", "ed", "es", "s"):
        if word.endswith(suffix) and len(word) - len(suffix) >= 3:
            if suffix in {"ies", "ied"}:
                return word[: -len(suffix)] + "y"
            if suffix == "es" and not word.endswith(("ses", "xes", "zes", "ches", "shes")):
                return word[:-1]
            if suffix == "s" and word.endswith("ss"):
                return word
            return word[: -len(suffix)]
    return word


# Research snippets rarely use the asker's word for the aspect ("where was it
# filmed?" vs "principal photography took place in Budapest"). A question word
# from one group adds the group's other words at a lower weight. The groups are
# the aspects film research stores (see MovieTools._persist_research).
_ASPECTS = [
    "music score soundtrack composer composed song theme orchestra",
    "filmed filming shot shooting location photography studio set",
    "cinematography cinematographer visual look colour color palette lighting light camera imax lens",
    "critic critics review reviews reviewer reviewers reception criticism praised acclaimed panned",
    "budget cost costs box office gross grossed earned money million revenue",
    "oscar oscars award awards academy won win nominated nomination prize",
]
_EXPANSION_WEIGHT = 0.4


def search_terms(text: str) -> list[str]:
    return [
        _stem(word)
        for word in re.findall(r"[^\W_]+(?:'[^\W_]+)?", (text or "").casefold())
        if word not in _STOPWORDS and (len(word) > 1 or word.isdigit())
    ]


_ASPECT_STEMS = [frozenset(search_terms(group)) for group in _ASPECTS]


def query_terms(q: str) -> tuple[list[str], list[frozenset[str]]]:
    """Stemmed query words, and the aspect groups they belong to (minus those words)."""
    words = list(dict.fromkeys(search_terms(q)))
    groups = [group - set(words) for group in _ASPECT_STEMS if group & set(words)]
    return words, [g for g in groups if g]


class RAGService:
    """Persistent hybrid retrieval: deterministic chunking, lexical/vector fusion,
    source-aware reranking and lightweight diversity selection. The vector path is
    optional; deployments without embeddings safely fall back to lexical retrieval."""

    def __init__(self, db):
        self.db = db

    @staticmethod
    def chunk(text, size=220, overlap=40):
        words = text.split()
        chunks = []
        start = 0
        if overlap >= size:
            overlap = max(0, size // 5)
        while start < len(words):
            end = min(len(words), start + size)
            chunk = " ".join(words[start:end]).strip()
            if chunk:
                chunks.append(chunk)
            if end >= len(words):
                break
            start = end - overlap
        return chunks

    def _embed(self, text):
        try:
            from app.embeddings import EmbeddingService

            return EmbeddingService.encode(text)
        except ImportError:
            log_event(
                "rag_embedding_fallback",
                level=logging.WARNING,
                details={"reason": "optional_dependency_missing"},
            )
            return None

    def ingest_many(self, documents, embed=True):
        created = []
        for item in documents:
            text = (item.get("text") or "").strip()
            if not text:
                continue
            chunks = self.chunk(text)
            for idx, chunk in enumerate(chunks):
                data = {k: v for k, v in item.items() if k not in {"metadata_json", "text", "embedding"}}
                data["text"] = chunk
                meta = {
                    **dict(item.get("metadata_json") or {}),
                    "chunk_index": idx,
                    "chunk_count": len(chunks),
                }
                data["metadata_json"] = meta
                data["content_hash"] = hashlib.sha256(chunk.encode("utf-8")).hexdigest()
                existing = self.db.scalar(
                    select(KnowledgeDocument.id).where(
                        KnowledgeDocument.content_hash == data["content_hash"],
                        KnowledgeDocument.url == data.get("url"),
                    )
                )
                if existing is not None:
                    continue
                if embed:
                    data["embedding"] = self._embed(chunk)
                obj = KnowledgeDocument(**data)
                self.db.add(obj)
                created.append(obj)
        if created:
            self.db.commit()
            for x in created:
                self.db.refresh(x)
        return created

    def ingest(self, **kw):
        created = self.ingest_many([kw], embed=True)
        if not created:
            raise ValueError("Knowledge text cannot be empty.")
        return created[0]

    @staticmethod
    def _lexical(docs, q, k1=1.2, b=0.75):
        """BM25 over the candidate documents, on stemmed words without stopwords.

        The film's own title is in every snippet about it, so title words score
        little (low IDF) and the words that say *what* is asked decide the rank.
        Returns (document, normalised score in 0..1), best first.
        """
        import math

        words, groups = query_terms(q)
        if not words or not docs:
            return []
        # The film's title is a field of every snippet, even when the text doesn't repeat it.
        bodies = [search_terms(f"{(d.metadata_json or {}).get('title') or ''} {d.text}") for d in docs]
        average = sum(len(body) for body in bodies) / len(bodies) or 1.0
        vocabulary = set(words).union(*groups)
        frequency = {t: sum(1 for body in bodies if t in body) for t in vocabulary}
        phrase = q.casefold().strip()
        ranked = []
        for d, body in zip(docs, bodies, strict=True):

            def term_score(t, body=body):
                count = body.count(t)
                if not count:
                    return 0.0
                idf = math.log(1 + (len(docs) - frequency[t] + 0.5) / (frequency[t] + 0.5))
                return idf * count * (k1 + 1) / (count + k1 * (1 - b + b * len(body) / average))

            score = sum(term_score(t) for t in words)
            # An aspect counts once, through its best word: synonyms don't add up.
            score += sum(_EXPANSION_WEIGHT * max(term_score(t) for t in group) for group in groups)
            if score and len(phrase) > 12 and phrase in d.text.casefold():
                score *= 1.2
            if score:
                ranked.append((score, d))
        ranked.sort(key=lambda x: (x[0], x[1].created_at), reverse=True)
        best = ranked[0][0] if ranked else 1.0
        return [(d, score / best) for score, d in ranked]

    def _rerank(self, items, q, limit):
        qterms = set(search_terms(q))
        scored = []
        for item in items:
            terms = set(search_terms(item["text"]))
            overlap = len(qterms & terms) / max(1, len(qterms))
            source_bonus = {
                "primary": 0.18,
                "official": 0.16,
                "review": 0.08,
                "interview": 0.08,
                "web": 0,
            }.get(item["source_type"], 0)
            freshness = 0.04 if item.get("published_at") else 0
            score = float(item["score"]) + 0.7 * item.get("lexical", 0.0) + 0.3 * overlap
            score += source_bonus + freshness
            scored.append((score, item))
        scored.sort(key=lambda z: z[0], reverse=True)
        # MMR-like source diversity: don't return ten snippets from one host.
        selected = []
        hosts = set()
        while scored and len(selected) < limit:
            best_i = 0
            best_score = -1e9
            for i, (score, item) in enumerate(scored[: min(30, len(scored))]):
                from urllib.parse import urlparse

                host = urlparse(item.get("url") or "").netloc.casefold()
                novelty = 0.08 if host and host not in hosts else 0
                val = score + novelty
                if val > best_score:
                    best_score = val
                    best_i = i
            score, item = scored.pop(best_i)
            selected.append(item)
            host = urlparse(item.get("url") or "").netloc.casefold()
            if host:
                hosts.add(host)
        return selected

    def search(self, q, movie_id=None, limit=8):
        q = (q or "").strip()
        if not q:
            return []
        stmt = select(KnowledgeDocument)
        if movie_id is not None:
            stmt = stmt.where(KnowledgeDocument.movie_id == movie_id)
        docs = self.db.scalars(stmt.order_by(KnowledgeDocument.created_at.desc()).limit(5000)).all()
        lex = self._lexical(docs, q)
        vec = []
        if Vector is not None and docs and any(getattr(d, "embedding", None) is not None for d in docs):
            query_vec = self._embed(q)
            if query_vec:
                vstmt = select(KnowledgeDocument).where(KnowledgeDocument.embedding.is_not(None))
                if movie_id is not None:
                    vstmt = vstmt.where(KnowledgeDocument.movie_id == movie_id)
                vec = self.db.scalars(
                    vstmt.order_by(KnowledgeDocument.embedding.cosine_distance(query_vec)).limit(
                        max(limit * 8, 40)
                    )
                ).all()
        scores = {}
        lexical = {}
        for rank, (d, normalised) in enumerate(lex[: max(limit * 8, 40)], 1):
            scores.setdefault(d.id, [0, d])[0] += 1 / (60 + rank)
            lexical[d.id] = normalised
        for rank, d in enumerate(vec, 1):
            scores.setdefault(d.id, [0, d])[0] += 1 / (60 + rank)
        candidates = []
        for score, d in sorted(scores.values(), key=lambda x: x[0], reverse=True)[: max(limit * 6, 30)]:
            candidates.append(
                {
                    "id": d.id,
                    "movie_id": d.movie_id,
                    "source": d.source,
                    "source_type": d.source_type,
                    "document_type": d.document_type,
                    "author": d.author,
                    "url": d.url,
                    "published_at": d.published_at,
                    "text": d.text,
                    "score": round(float(score), 6),
                    "lexical": round(lexical.get(d.id, 0.0), 4),
                    "has_embedding": d.embedding is not None,
                    "metadata": d.metadata_json or {},
                }
            )
        return self._rerank(candidates, q, limit)
