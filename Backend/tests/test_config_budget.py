import pytest
from pydantic import ValidationError

from app.config import Settings


def _base(**overrides):
    values = {
        "db_pool_size": 5,
        "db_max_overflow": 5,
        "db_worker_count": 2,
        "db_replica_count": 1,
        "db_connection_budget": 20,
    }
    values.update(overrides)
    return values


def test_connection_budget_accepts_exact_capacity():
    settings = Settings(**_base())
    assert (
        settings.db_replica_count
        * settings.db_worker_count
        * (settings.db_pool_size + settings.db_max_overflow)
        == 20
    )


def test_connection_budget_rejects_over_capacity():
    with pytest.raises(ValidationError, match="connection budget is too small"):
        Settings(**_base(db_connection_budget=19))


def test_connection_budget_scales_across_replicas_and_workers():
    with pytest.raises(ValidationError, match="need 80"):
        Settings(**_base(db_worker_count=4, db_replica_count=2, db_connection_budget=39))
