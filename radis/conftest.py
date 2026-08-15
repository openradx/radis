import pytest

from radis.pgsearch.utils.language_utils import clear_search_config_cache

pytest_plugins = ["adit_radis_shared.pytest_fixtures"]


@pytest.fixture(autouse=True)
def _clear_language_config_cache():
    """code_to_language and get_available_search_configs
    (radis.pgsearch.utils.language_utils) are process-global caches, and
    resolving a language code is not confined to pgsearch's own tests --
    radis.pgsearch.providers._language_configs runs on every hybrid search, so
    any test anywhere in this suite that seeds a Report (and therefore a
    Language) can populate or observe these caches. radis/pgsearch/tests/
    test_language_utils.py monkeypatches the available config set per test and
    reuses codes (e.g. 'tr', 'zh') across tests that expect DIFFERENT
    resolutions under different mocked config sets; without a reset at this
    scope, whichever test runs first for a given code poisons every later
    test -- in any app's test suite, not just pgsearch's -- that resolves the
    same code. This was verified directly: with this reset scoped only to
    radis/pgsearch/tests/conftest.py, running test_language_utils.py before
    radis/search/tests/ produced no failures only because every test in that
    directory happens to use language code 'en' with a mocked-vs-real answer
    that happens to agree -- a dormant collision, not a fixed one.

    This lives at the project root on purpose, not in
    radis/pgsearch/tests/conftest.py where the caches themselves are defined:
    a cache's test reset belongs wherever its blast radius reaches, not
    wherever the test that happened to discover the problem lives. Process-
    global state in this codebase has previously had its reset fixture scoped
    to the wrong place more than once -- if you add another module-level
    cache, scope its reset the same way from the start.
    """
    clear_search_config_cache()
    yield
    clear_search_config_cache()
