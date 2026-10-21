from app.memory import MemoryService


def test_memory_retrieval_does_not_cold_load_embeddings_by_default(monkeypatch):
    class FakeEmbeddingService:
        _model = None

        @classmethod
        def encode(cls, text):
            raise AssertionError("embedding model must not cold-load during ordinary chat memory context")

    import app.embeddings as embeddings

    monkeypatch.setattr(embeddings, "EmbeddingService", FakeEmbeddingService)

    class DummyDB:
        class _Bind:
            class _Dialect:
                name = "sqlite"

            dialect = _Dialect()

        def get_bind(self):
            return self._Bind()

        def commit(self):
            pass

        def scalar(self, stmt):
            return None

        def scalars(self, stmt):
            class Result:
                def all(self):
                    return []

            return Result()

    assert MemoryService(DummyDB()).memories(1, "Who directed Interstellar?", 8) == []
