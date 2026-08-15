import pytest
from django.core.cache import cache

from radis.pgsearch import providers
from radis.pgsearch.utils.language_utils import clear_search_config_cache


@pytest.fixture(autouse=True)
def _clear_language_config_cache():
    """code_to_language and get_available_search_configs (language_utils.py) are
    process-global lru_caches. test_language_utils.py monkeypatches the config
    set per test and reuses codes (e.g. 'tr', 'zh') across tests that expect
    DIFFERENT resolutions under different mocked config sets -- without this,
    whichever test runs first for a given code would poison every later test,
    in this file or elsewhere in the suite, that resolves the same code."""
    clear_search_config_cache()
    yield
    clear_search_config_cache()


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
