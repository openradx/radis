import pytest
from django.core.cache import cache

from radis.pgsearch import providers


@pytest.fixture(autouse=True)
def _clear_cache():
    """Query embeddings are cached across requests (see providers._embed_query_cached);
    the process-local test cache would otherwise leak vectors between tests that mock
    the same query with different embeddings."""
    cache.clear()
    yield
    cache.clear()


@pytest.fixture(autouse=True)
def _clear_logged_permanent_failure_configs():
    """_embed_query_or_none throttles permanent-failure tracebacks to once per
    (EMBEDDINGS_BASE_URL, model) configuration (see providers.py); the process-local
    set would otherwise leak between tests that hit the same config, making a later
    test's assertion on log level depend on test order."""
    providers._LOGGED_PERMANENT_FAILURE_CONFIGS.clear()
    yield
    providers._LOGGED_PERMANENT_FAILURE_CONFIGS.clear()
