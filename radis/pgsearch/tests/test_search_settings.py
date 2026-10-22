import os

import pytest
from django.conf import settings

# The fallback each setting takes when its environment variable is absent.
EXPECTED_DEFAULTS = {
    "SEARCH_HNSW_EF_SEARCH": 500,
    "SEARCH_RANKING_CACHE_TIMEOUT_SECONDS": 300,
    "SEARCH_RERANK_WINDOW": 1000,
    "SEARCH_RERANK_SEMANTIC_WEIGHT": 0.7,
    "SEARCH_SEMANTIC_ITERATIVE_SCAN": True,
    "SEARCH_SEMANTIC_MIN_SIMILARITY": None,
    "EMBEDDINGS_QUERY_REQUEST_TIMEOUT_SECONDS": 10.0,
    "SEARCH_DESCRIPTION_TIMEOUT": 10,
    "SEARCH_DESCRIPTION_MAX_TOKENS": 200,
    "SEARCH_DESCRIPTION_CACHE_TIMEOUT_SECONDS": 3600,
}


def _skip_when_configured(name: str) -> None:
    # Only the fallback is under test. A value configured for the environment the tests
    # run in (a developer's .env, say) is a deployment choice, not a regression. A blank
    # counts as unset, which is what an `.env` line with nothing after the '=' produces.
    if os.environ.get(name, "").strip():
        pytest.skip(f"{name} is set in the environment, so its default is not in play")


def test_the_full_text_cap_is_ten_thousand_hits():
    assert settings.SEARCH_FTS_MAX_RESULTS == 10_000


@pytest.mark.parametrize(("name", "expected"), EXPECTED_DEFAULTS.items())
def test_search_settings_have_expected_defaults(name: str, expected):
    _skip_when_configured(name)

    assert getattr(settings, name) == expected


def test_the_description_prompt_has_a_built_in_default():
    _skip_when_configured("SEARCH_DESCRIPTION_SYSTEM_PROMPT")

    assert "radiology reports" in settings.SEARCH_DESCRIPTION_SYSTEM_PROMPT


def test_the_beam_width_rides_on_the_connection_options():
    options = settings.DATABASES["default"]["OPTIONS"]["options"]
    assert options == f"-c hnsw.ef_search={settings.SEARCH_HNSW_EF_SEARCH}"


def test_no_hybrid_setting_remains():
    # The names are assembled so that a repository search for the old prefix only finds
    # settings that were left behind, not this list of the ones that must be gone.
    prefix = "HYBRID"
    for suffix in ("FTS_MAX_RESULTS", "RRF_K", "HNSW_EF_SEARCH", "FUSED_CACHE_TIMEOUT_SECONDS"):
        name = f"{prefix}_{suffix}"
        assert not hasattr(settings, name), name
