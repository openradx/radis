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


def test_no_model_configured_means_hybrid_search_is_off(clean_embeddings_env):
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
def _restore_base_settings_after_reload():
    """`EMBEDDINGS_QUERY_INSTRUCTION` is a bare module-level assignment (like its
    `EMBEDDINGS_DIM`/`EMBEDDINGS_BATCH_SIZE` neighbours), not a function, so the only way
    to observe it re-read a monkeypatched env var is to reload the settings module. That
    only recomputes `radis.settings.base`'s own namespace — `django.conf.settings` already
    took its one-time snapshot at Django startup, so this can't corrupt the live app config
    for other tests. Still reload back to the real environment afterwards so this test's
    patch doesn't leak into the module object for whatever runs next in this process."""
    yield
    importlib.reload(settings_base)


def test_embeddings_query_instruction_is_read_from_the_environment(
    monkeypatch, _restore_base_settings_after_reload
):
    monkeypatch.setenv("EMBEDDINGS_QUERY_INSTRUCTION", "Represent this radiology search query: ")

    reloaded = importlib.reload(settings_base)

    assert reloaded.EMBEDDINGS_QUERY_INSTRUCTION == "Represent this radiology search query: "


def test_embeddings_query_instruction_default_survives_when_unset(
    monkeypatch, _restore_base_settings_after_reload
):
    monkeypatch.delenv("EMBEDDINGS_QUERY_INSTRUCTION", raising=False)

    reloaded = importlib.reload(settings_base)

    assert reloaded.EMBEDDINGS_QUERY_INSTRUCTION == DEFAULT_QUERY_INSTRUCTION


def test_the_base_url_defaults_to_the_llm_endpoint():
    # Only the fallback is under test; a value configured for this environment is a
    # deployment choice, not a regression.
    import os

    if os.environ.get("EMBEDDINGS_BASE_URL", "").strip():
        pytest.skip("EMBEDDINGS_BASE_URL is set in the environment, so its default is not in play")

    assert dj_settings.EMBEDDINGS_BASE_URL == dj_settings.LLM_BASE_URL
