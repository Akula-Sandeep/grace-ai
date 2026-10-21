"""MEM-001 benchmark on PostgreSQL: the numbers are recorded in the audit; floors guard regressions."""

import json
import os
import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base, User
from app.evals.memory_recall import run


def _run(distractors):
    engine = create_engine(os.environ["POSTGRES_TEST_DATABASE_URL"])
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine)() as db:
        user = User(username=f"recall-{uuid.uuid4().hex[:8]}", password_hash="x")
        db.add(user)
        db.commit()
        report = run(db, user.id, distractors=distractors)
    print(f"\nMEMORY_RECALL stored={report['memories_stored']} " + json.dumps(report["by_kind"]))
    return report


@pytest.mark.skipif(not os.getenv("POSTGRES_TEST_DATABASE_URL"), reason="needs PostgreSQL full-text search")
def test_memory_recall_benchmark():
    small = _run(4)  # a typical user: ten facts and a few other memories
    large = _run(50)  # a heavy user: sixty memories
    # Shared words are always found (CTX-002).
    for report in (small, large):
        keyword = report["by_kind"]["keyword"]
        assert keyword["search_hit"] == keyword["cases"]
    # A typical user's facts all reach the model, whatever the wording or language.
    assert all(row["in_prompt"] for row in small["rows"])
    # A heavy user's facts are past the prompt bound; the recall tool finds each one
    # from the keywords a model would write (the rewrite itself is checked live only).
    assert all(row["recall_hit"] for row in large["rows"])
