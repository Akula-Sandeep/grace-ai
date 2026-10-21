"""Suite-wide test isolation.

Each test builds its own database, and SQLite restarts primary keys at 1, so
process-wide in-memory caches keyed by user id (taste profiles, guest state,
rate limits) must not leak between tests.
"""

import pytest

from app import shared_state as shared_state_module
from app.services.wikidata_client import WikidataClient


@pytest.fixture(autouse=True)
def _isolate_in_memory_shared_state():
    state = shared_state_module.shared_state
    if isinstance(state._backend, shared_state_module._InMemoryBackend):
        state._backend = shared_state_module._InMemoryBackend()
    yield


@pytest.fixture(autouse=True)
def _isolate_wikidata_result_cache():
    """WikidataClient's successful-query cache is a class-level dict, shared
    across every WikidataClient() instance so a lookup outlives one request
    (see wikidata_client.py). Several tests reuse the same literal query
    text ("query", "SELECT * WHERE {}"), so without resetting this between
    tests, one test's cached success could silently serve as another test's
    "network" response.
    """
    WikidataClient._RESULT_CACHE.clear()
    WikidataClient._locks.clear()
    yield
    WikidataClient._RESULT_CACHE.clear()
    WikidataClient._locks.clear()


@pytest.fixture(autouse=True)
def _isolate_provider_resilience_state():
    """Circuit breakers, model cooldowns and provider health are process-wide by
    design (they protect every request); one test's simulated outage must not
    fail the next test fast."""
    from app.inference.providers import chain
    from app.services import provider_health, resilience

    resilience.reset()
    chain._COOLDOWN_UNTIL.clear()
    chain._COOLDOWN_REASON.clear()
    provider_health.reset()
    yield
    resilience.reset()
    chain._COOLDOWN_UNTIL.clear()
    chain._COOLDOWN_REASON.clear()
    provider_health.reset()
