import pytest
from django.core.cache import cache


@pytest.fixture(autouse=True)
def _clear_cache():
    """The ranking and query-embedding caches use the process-local test cache, which
    outlives individual tests; an entry cached by one test must not serve the next."""
    cache.clear()
    yield
    cache.clear()
