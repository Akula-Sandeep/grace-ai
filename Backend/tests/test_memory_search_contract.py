from types import SimpleNamespace

from sqlalchemy.dialects import postgresql

from app.memory import MemoryService


def test_postgres_history_query_uses_full_text_search():
    captured = []
    fake_db = SimpleNamespace(
        get_bind=lambda: SimpleNamespace(dialect=postgresql.dialect()),
        execute=lambda statement: captured.append(statement) or SimpleNamespace(all=lambda: []),
    )
    service = MemoryService(fake_db)

    assert service.relevant_history(1, "thoughtful science fiction", limit=4) == []
    compiled = captured[0].compile(dialect=postgresql.dialect())
    sql = str(compiled)
    assert "to_tsvector" in sql
    # Any significant word may match (ranked); "plainto_tsquery" required all of them.
    assert "to_tsquery" in sql and "plainto_tsquery" not in sql
    assert "thoughtful | science | fiction" in compiled.params.values()
    assert "@@" in sql
    assert "LIMIT" in sql


def test_postgres_memory_query_uses_full_text_search_without_vector_support(
    monkeypatch,
):
    captured = []
    fake_db = SimpleNamespace(
        get_bind=lambda: SimpleNamespace(dialect=postgresql.dialect()),
        scalar=lambda statement: None,
        execute=lambda statement: captured.append(statement) or SimpleNamespace(all=lambda: []),
    )
    service = MemoryService(fake_db)
    monkeypatch.setattr(service, "_embedding", lambda _text, load=False: None)

    assert service.memories(1, "thoughtful science fiction", limit=4) == []
    compiled = captured[0].compile(dialect=postgresql.dialect())
    sql = str(compiled)
    assert "to_tsvector" in sql
    assert "to_tsquery" in sql and "plainto_tsquery" not in sql
    assert "thoughtful | science | fiction" in compiled.params.values()
    assert "@@" in sql
    assert "LIMIT" in sql


def test_memory_search_migration_defines_postgres_fts_and_vector_indexes(monkeypatch):
    import importlib.util
    from pathlib import Path

    migration_path = Path(__file__).parents[1] / "alembic" / "versions" / "0020_memory_search_indexes.py"
    spec = importlib.util.spec_from_file_location("memory_search_migration", migration_path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)

    statements = []
    monkeypatch.setattr(
        module.op,
        "get_bind",
        lambda: SimpleNamespace(dialect=SimpleNamespace(name="postgresql")),
    )
    monkeypatch.setattr(module.op, "execute", lambda statement: statements.append(str(statement)))

    module.upgrade()

    assert any(
        "ix_messages_content_fts" in statement and "to_tsvector('simple', content)" in statement
        for statement in statements
    )
    assert any(
        "ix_memories_content_fts" in statement and "to_tsvector('simple', content)" in statement
        for statement in statements
    )
    assert any(
        "ix_memories_embedding_hnsw" in statement and "vector_cosine_ops" in statement
        for statement in statements
    )


def test_a_natural_question_recalls_a_sentence_that_shares_only_some_words():
    """The audit's CTX-002 case: every word of the question was required before."""
    from app.memory import any_terms_query

    query = any_terms_query(
        "given everything I told you at the start, which version of Arrival should I watch tonight?"
    )
    assert query is not None and " | " in query
    assert "arrival" in query.split(" | ") and "the" not in query.split(" | ")
    assert any_terms_query("it is the") is None
    # Only word characters reach to_tsquery, split the way Postgres splits them.
    assert any_terms_query("o'brien's (2019) & | ! :*") == "brien | 2019"
