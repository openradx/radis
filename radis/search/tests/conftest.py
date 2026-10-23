import pytest
from django.core.cache import cache


@pytest.fixture(autouse=True)
def _clear_cache():
    """The description writer and the ranking cache use the process-local test cache,
    which outlives individual tests; a cached answer from one test must not serve the
    next."""
    cache.clear()
    yield
    cache.clear()
