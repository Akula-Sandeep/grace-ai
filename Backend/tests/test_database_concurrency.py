import pytest

from app.database import engine


@pytest.mark.skipif(engine.dialect.name != "sqlite", reason="SQLite-only connection settings")
def test_sqlite_connections_enable_wal_and_busy_timeout():
    with engine.connect() as connection:
        journal_mode = connection.exec_driver_sql("PRAGMA journal_mode").scalar_one().lower()
        synchronous = connection.exec_driver_sql("PRAGMA synchronous").scalar_one()
        busy_timeout = connection.exec_driver_sql("PRAGMA busy_timeout").scalar_one()

    assert journal_mode in {"wal", "memory"}
    assert synchronous == 1
    assert busy_timeout == 5000
