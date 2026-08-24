import pytest

from radis.pgsearch.utils.language_utils import clear_search_config_cache

pytest_plugins = ["adit_radis_shared.pytest_fixtures"]


@pytest.fixture(autouse=True)
def _clear_language_config_cache():
    """Reset the process-global language-config caches between tests.

    ``test_language_utils.py`` mocks the available config set per test and
    reuses codes across tests that expect different resolutions, so without a
    reset the first test to resolve a code fixes the answer for every later
    one. This lives at the project root rather than in pgsearch's conftest
    because any test that seeds a Report resolves a language code, so the
    caches are reachable from every app's tests -- a reset belongs wherever
    the state reaches, not where the problem was found.
    """
    clear_search_config_cache()
    yield
    clear_search_config_cache()
