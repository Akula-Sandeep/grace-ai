import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateIndex

from app.database import Base, init_db


def test_hot_query_indexes_are_in_model_metadata():
    expected = {
        "conversations": {"ix_conversations_user_visibility_order"},
        "messages": {"ix_messages_convo_deleted_created_id"},
        "movie_references": {
            "ix_movie_references_user_created_id",
            "ix_movie_references_conversation_created_id",
        },
    }
    for table_name, index_names in expected.items():
        actual = {index.name for index in Base.metadata.tables[table_name].indexes}
        assert index_names <= actual

    compiled = {
        index.name: str(CreateIndex(index).compile(dialect=postgresql.dialect()))
        for table_name in expected
        for index in Base.metadata.tables[table_name].indexes
        if index.name in set.union(*expected.values())
    }
    assert "is_pinned DESC, updated_at DESC, id DESC" in compiled["ix_conversations_user_visibility_order"]
    assert "created_at DESC, id DESC" in compiled["ix_messages_convo_deleted_created_id"]
    assert "created_at DESC, id DESC" in compiled["ix_movie_references_user_created_id"]
    assert "created_at DESC, id DESC" in compiled["ix_movie_references_conversation_created_id"]


def test_runtime_schema_creation_requires_alembic():
    with pytest.raises(RuntimeError, match="alembic upgrade head"):
        init_db()
