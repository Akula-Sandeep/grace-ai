"""Exercise the PostgreSQL migration path on a dedicated disposable database."""

import os
import sys
from pathlib import Path

from alembic.config import Config
from sqlalchemy import create_engine, inspect, text

from alembic import command

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import argparse

from app.config import settings

PREVIOUS_RELEASE = "0010_chat_runtime"
EXPECTED_INDEXES = (
    ("conversations", "ix_conversations_user_visibility_order"),
    ("messages", "ix_messages_convo_deleted_created_id"),
    ("movie_references", "ix_movie_references_user_created_id"),
    ("movie_references", "ix_movie_references_conversation_created_id"),
)


def _assert_indexes(database_url: str, present: bool) -> None:
    engine = create_engine(database_url)
    try:
        inspector = inspect(engine)
        for table, index_name in EXPECTED_INDEXES:
            found = {index["name"] for index in inspector.get_indexes(table)}
            if (index_name in found) is not present:
                raise RuntimeError(
                    f"{index_name} on {table}: expected present={present}, " f"found={index_name in found}"
                )
        if present:
            with engine.connect() as connection:
                indexes = connection.execute(
                    text("SELECT indexname, indexdef FROM pg_indexes WHERE schemaname = current_schema()")
                ).all()
            definitions = {name: definition for name, definition in indexes}
            for table, index_name in EXPECTED_INDEXES:
                if index_name not in definitions:
                    raise RuntimeError(f"{index_name} is missing from pg_indexes")
                if "created_at" not in definitions[index_name] and table != "conversations":
                    raise RuntimeError(f"{index_name} is missing its created_at key")
                if "id DESC" not in definitions[index_name]:
                    raise RuntimeError(f"{index_name} is missing its id tie-breaker")
    finally:
        engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--database-url",
        default=os.environ.get("DATABASE_URL"),
        help="PostgreSQL URL for a dedicated disposable database (or set DATABASE_URL).",
    )
    parser.add_argument(
        "--yes-destroy-schema",
        action="store_true",
        help="Confirm that this script may downgrade the target database to base.",
    )
    args = parser.parse_args()
    if not args.yes_destroy_schema:
        parser.error("refusing destructive verification without --yes-destroy-schema")
    if not args.database_url:
        parser.error("provide --database-url or DATABASE_URL")
    if not args.database_url.startswith(("postgresql://", "postgresql+psycopg://")):
        parser.error("migration verification requires PostgreSQL")

    settings.database_url = args.database_url
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))

    command.downgrade(config, "base")
    command.upgrade(config, "head")
    _assert_indexes(args.database_url, present=True)

    command.downgrade(config, "base")
    command.upgrade(config, PREVIOUS_RELEASE)
    command.upgrade(config, "head")
    _assert_indexes(args.database_url, present=True)
    command.downgrade(config, PREVIOUS_RELEASE)
    command.upgrade(config, "head")
    _assert_indexes(args.database_url, present=True)
    print(
        "Migration verification passed: empty -> head, previous release -> head, head -> previous release -> head."
    )


if __name__ == "__main__":
    main()
