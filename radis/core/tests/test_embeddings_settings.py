import importlib

import pytest
from django.conf import settings as dj_settings
from django.core.exceptions import ImproperlyConfigured

from radis.settings import base as settings_base
from radis.settings.base import _inherit_env, _resolve_embeddings_model

DEFAULT_QUERY_INSTRUCTION = (
    "Instruct: Given a radiology search query, retrieve relevant radiology reports.\nQuery: "
)


@pytest.fixture
def clean_embeddings_env(monkeypatch):
    """Start from no embedding configuration at all."""
    for name in ("EMBEDDINGS_MODEL", "EMBEDDINGS_BASE_URL", "EMBEDDINGS_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def test_an_unset_setting_inherits_its_llm_counterpart(clean_embeddings_env):
    assert _inherit_env("EMBEDDINGS_BASE_URL", "https://llm.example/v1") == "https://llm.example/v1"


@pytest.mark.parametrize("raw", ["", "   "])
def test_a_blank_setting_counts_as_unset(clean_embeddings_env, raw: str):
    # A compose file passes every variable through, empty when it is not configured.
    clean_embeddings_env.setenv("EMBEDDINGS_BASE_URL", raw)

    assert _inherit_env("EMBEDDINGS_BASE_URL", "https://llm.example/v1") == "https://llm.example/v1"


def test_a_configured_setting_overrides_the_llm_counterpart(clean_embeddings_env):
    # The split case: a self-hosted vLLM serves one model per process, so the
    # embedding endpoint is a different server than the chat endpoint.
    clean_embeddings_env.setenv("EMBEDDINGS_BASE_URL", "https://embed.example/v1")

    assert (
        _inherit_env("EMBEDDINGS_BASE_URL", "https://llm.example/v1") == "https://embed.example/v1"
    )


def test_no_model_configured_means_semantic_search_is_off(clean_embeddings_env):
    assert _resolve_embeddings_model() is None


def test_a_blank_model_counts_as_unset(clean_embeddings_env):
    clean_embeddings_env.setenv("EMBEDDINGS_MODEL", "   ")

    assert _resolve_embeddings_model() is None


def test_the_model_spec_carries_request_parameters(clean_embeddings_env):
    clean_embeddings_env.setenv("EMBEDDINGS_MODEL", "text-embedding-3-large?dimensions=1024")

    spec = _resolve_embeddings_model()

    assert spec is not None
    assert spec.model == "text-embedding-3-large"
    assert spec.params == {"dimensions": 1024}


def test_a_malformed_model_names_the_setting_at_fault(clean_embeddings_env):
    # A bare ModelSpecError would leave the admin guessing which variable is wrong.
    clean_embeddings_env.setenv("EMBEDDINGS_MODEL", "?dimensions=1024")

    with pytest.raises(ImproperlyConfigured, match="EMBEDDINGS_MODEL"):
        _resolve_embeddings_model()


@pytest.fixture
def _restore_base_settings_after_reload(monkeypatch):
    """`EMBEDDINGS_QUERY_INSTRUCTION` is a bare module-level assignment (like its
    `EMBEDDINGS_DIM`/`EMBEDDINGS_BATCH_SIZE` neighbours), not a function, so the only way
    to observe it re-read a monkeypatched env var is to reload the settings module. That
    only recomputes `radis.settings.base`'s own namespace — `django.conf.settings` already
    took its one-time snapshot at Django startup, so this can't corrupt the live app config
    for other tests. Still reload back to the real environment afterwards so this test's
    patch doesn't leak into the module object for whatever runs next in this process.

    It undoes the patched environment itself before that reload. pytest-django's autouse
    fixtures set `monkeypatch` up before any fixture a test requests, so no argument order
    makes it the last one torn down; a reload that still saw the patched variables would
    re-bake them into the module, or raise when a test patched one to an invalid value."""
    yield
    monkeypatch.undo()
    importlib.reload(settings_base)


def test_embeddings_query_instruction_is_read_from_the_environment(
    _restore_base_settings_after_reload, monkeypatch
):
    monkeypatch.setenv("EMBEDDINGS_QUERY_INSTRUCTION", "Represent this radiology search query: ")

    reloaded = importlib.reload(settings_base)

    assert reloaded.EMBEDDINGS_QUERY_INSTRUCTION == "Represent this radiology search query: "


def test_embeddings_query_instruction_default_survives_when_unset(
    _restore_base_settings_after_reload, monkeypatch
):
    monkeypatch.delenv("EMBEDDINGS_QUERY_INSTRUCTION", raising=False)

    reloaded = importlib.reload(settings_base)

    assert reloaded.EMBEDDINGS_QUERY_INSTRUCTION == DEFAULT_QUERY_INSTRUCTION


@pytest.mark.parametrize(
    ("embeddings_name", "llm_name"),
    [
        ("EMBEDDINGS_BASE_URL", "LLM_BASE_URL"),
        ("EMBEDDINGS_API_KEY", "LLM_API_KEY"),
        ("EMBEDDINGS_REQUEST_TIMEOUT_SECONDS", "LLM_REQUEST_TIMEOUT_SECONDS"),
    ],
)
def test_the_setting_defaults_to_its_llm_counterpart(embeddings_name: str, llm_name: str):
    # Only the fallback is under test; a value configured for this environment is a
    # deployment choice, not a regression.
    import os

    if os.environ.get(embeddings_name, "").strip():
        pytest.skip(f"{embeddings_name} is set in the environment, so its default is not in play")

    assert getattr(dj_settings, embeddings_name) == getattr(dj_settings, llm_name)


# The search settings are plain module-level assignments, so their boot-time checks only
# run when the settings module is imported: each case below reloads it with the variable
# patched. They are tested here because the reload fixture is local to this module.

# What each setting falls back to when its variable is blank.
SEARCH_SETTING_DEFAULTS = {
    "SEARCH_HNSW_EF_SEARCH": 500,
    "SEARCH_RANKING_CACHE_TIMEOUT_SECONDS": 300,
    "SEARCH_RERANK_WINDOW": 1000,
    "SEARCH_RERANK_SEMANTIC_WEIGHT": 0.7,
    "SEARCH_SEMANTIC_MIN_SIMILARITY": None,
    "EMBEDDINGS_QUERY_REQUEST_TIMEOUT_SECONDS": 10.0,
}


@pytest.mark.parametrize(
    ("name", "raw", "expected"),
    [
        ("SEARCH_HNSW_EF_SEARCH", "1", 1),
        ("SEARCH_HNSW_EF_SEARCH", "1000", 1000),
        ("SEARCH_RANKING_CACHE_TIMEOUT_SECONDS", "0", 0),
        ("SEARCH_RERANK_WINDOW", "1", 1),
        ("SEARCH_RERANK_SEMANTIC_WEIGHT", "0", 0.0),
        ("SEARCH_RERANK_SEMANTIC_WEIGHT", "1", 1.0),
        ("SEARCH_SEMANTIC_ITERATIVE_SCAN", "false", False),
        ("SEARCH_SEMANTIC_MIN_SIMILARITY", "0", 0.0),
        ("SEARCH_SEMANTIC_MIN_SIMILARITY", "0.35", 0.35),
        ("SEARCH_SEMANTIC_MIN_SIMILARITY", "1", 1.0),
        ("EMBEDDINGS_QUERY_REQUEST_TIMEOUT_SECONDS", "0.5", 0.5),
    ],
)
def test_a_valid_search_setting_is_read_from_the_environment(
    _restore_base_settings_after_reload, monkeypatch, name: str, raw: str, expected
):
    monkeypatch.setenv(name, raw)

    reloaded = importlib.reload(settings_base)

    assert getattr(reloaded, name) == expected


@pytest.mark.parametrize(
    ("name", "raw"),
    [
        ("SEARCH_HNSW_EF_SEARCH", "0"),
        ("SEARCH_HNSW_EF_SEARCH", "1001"),
        ("SEARCH_HNSW_EF_SEARCH", "many"),
        ("SEARCH_RANKING_CACHE_TIMEOUT_SECONDS", "-1"),
        ("SEARCH_RERANK_WINDOW", "0"),
        ("SEARCH_RERANK_SEMANTIC_WEIGHT", "1.1"),
        ("SEARCH_RERANK_SEMANTIC_WEIGHT", "-0.1"),
        ("SEARCH_RERANK_SEMANTIC_WEIGHT", "nan"),
        ("SEARCH_SEMANTIC_MIN_SIMILARITY", "2"),
        ("SEARCH_SEMANTIC_MIN_SIMILARITY", "-0.1"),
        ("SEARCH_SEMANTIC_MIN_SIMILARITY", "nan"),
        ("EMBEDDINGS_QUERY_REQUEST_TIMEOUT_SECONDS", "0"),
        ("EMBEDDINGS_QUERY_REQUEST_TIMEOUT_SECONDS", "-1"),
        ("EMBEDDINGS_QUERY_REQUEST_TIMEOUT_SECONDS", "nan"),
    ],
)
def test_an_invalid_search_setting_names_the_setting_at_fault(
    _restore_base_settings_after_reload, monkeypatch, name: str, raw: str
):
    # Otherwise it boots fine and every search fails at runtime.
    monkeypatch.setenv(name, raw)

    with pytest.raises(ImproperlyConfigured, match=name):
        importlib.reload(settings_base)


@pytest.mark.parametrize("raw", ["", "   "])
@pytest.mark.parametrize(("name", "default"), SEARCH_SETTING_DEFAULTS.items())
def test_a_blank_search_setting_counts_as_unset(
    _restore_base_settings_after_reload, monkeypatch, name: str, default, raw: str
):
    # An `.env` line with nothing after the '=' means "not configured".
    monkeypatch.setenv(name, raw)

    reloaded = importlib.reload(settings_base)

    assert getattr(reloaded, name) == default
