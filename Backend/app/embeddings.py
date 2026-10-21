from __future__ import annotations

import os

VECTOR_DIMENSION = 384


class EmbeddingDimensionError(ValueError):
    pass


class EmbeddingService:
    """Local Sentence-Transformers embedding adapter. Model is loaded lazily so direct
    movie questions never pay the RAG model startup cost."""

    _model = None

    @classmethod
    def model(cls):
        if cls._model is None:
            from sentence_transformers import SentenceTransformer

            cls._model = SentenceTransformer(os.getenv("EMBEDDING_MODEL", "BAAI/bge-small-en-v1.5"))
        return cls._model

    @classmethod
    def encode(cls, text):
        encoded = cls.model().encode(text, normalize_embeddings=True)
        vector = encoded.tolist() if hasattr(encoded, "tolist") else list(encoded)
        if len(vector) != VECTOR_DIMENSION:
            raise EmbeddingDimensionError(
                f"Embedding dimension mismatch: expected {VECTOR_DIMENSION}, received {len(vector)}."
            )
        return vector
