"""RAG-001 retrieval relevance on PostgreSQL; numbers are recorded in the audit."""

import json
import os

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.evals.rag_relevance import run


@pytest.mark.skipif(not os.getenv("POSTGRES_TEST_DATABASE_URL"), reason="needs PostgreSQL")
def test_rag_relevance_benchmark():
    engine = create_engine(os.environ["POSTGRES_TEST_DATABASE_URL"])
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine)() as db:
        report = run(db)
    print("\nRAG_RELEVANCE " + json.dumps(report["summary"]))
    for row in report["rows"]:
        if row["rank"] != 1:
            print("  miss", row)
    summary = report["summary"]
    # Floors from the run recorded in the audit (BM25 + aspect words); before: 10/18 and 4/10 at rank 1.
    assert summary["tuning, one film"]["hit_at_1"] >= 15
    assert summary["held out, one film"]["hit_at_1"] >= 9
    assert summary["held out, all films"]["mrr"] >= 0.85
