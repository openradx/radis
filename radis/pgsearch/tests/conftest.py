import pytest
from django.core.cache import cache


@pytest.fixture(autouse=True)
def _clear_cache():
    """Query embeddings are cached across requests (see providers._embed_query_cached);
    the process-local test cache would otherwise leak vectors between tests that mock
    the same query with different embeddings."""
    cache.clear()
    yield
    cache.clear()
