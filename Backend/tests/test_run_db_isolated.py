import threading

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import run_db


@pytest.mark.asyncio
async def test_run_db_isolates_sync_orm_work_from_event_loop_thread(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'run-db.db'}", connect_args={"check_same_thread": False})
    factory = sessionmaker(bind=engine)
    loop_thread = threading.get_ident()

    # Keep each session alive: comparing id() of a collected object is flaky (ids are reused).
    sessions = []

    def first(db):
        sessions.append(db)
        return {"thread": threading.get_ident(), "dialect": db.get_bind().dialect.name}

    observed = await run_db(first, session_factory=factory)

    assert observed["thread"] != loop_thread
    assert observed["dialect"] == "sqlite"

    await run_db(sessions.append, session_factory=factory)
    assert len(sessions) == 2 and sessions[0] is not sessions[1]
    engine.dispose()
