import os
from pathlib import Path

import pytest
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from testcontainers.community.postgres import PostgresContainer

from alembic import command

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_POSTGRES_CONTAINER_TESTS") != "1",
    reason="set RUN_POSTGRES_CONTAINER_TESTS=1 to run PostgreSQL container integration tests",
)


def test_postgres_container_migrates_and_serves_hot_query_indexes(monkeypatch):
    from app.config import settings

    with PostgresContainer(
        "pgvector/pgvector:pg16",
        username="movie_agent",
        password="test-only-password",
        dbname="movie_agent",
        driver="psycopg",
    ) as postgres:
        database_url = postgres.get_connection_url()
        monkeypatch.setattr(settings, "database_url", database_url)
        config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))

        command.upgrade(config, "head")

        engine = create_engine(database_url)
        try:
            inspector = inspect(engine)
            assert "users" in inspector.get_table_names()
            indexes = {index["name"] for index in inspector.get_indexes("conversations")}
            assert "ix_conversations_user_visibility_order" in indexes
            with engine.connect() as connection:
                assert connection.execute(text("SELECT 1")).scalar_one() == 1
                assert (
                    connection.execute(
                        text("SELECT extname FROM pg_extension WHERE extname = 'vector'")
                    ).scalar_one()
                    == "vector"
                )
            command.downgrade(config, "base")
            assert "users" not in inspect(engine).get_table_names()
        finally:
            engine.dispose()
