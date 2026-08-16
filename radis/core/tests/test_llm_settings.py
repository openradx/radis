import json
import os

import pytest
from django.conf import settings as dj_settings
from django.core.exceptions import ImproperlyConfigured

from radis.settings.base import LLM_FEATURES, _optional_env, _resolve_llm_models

# The fallback each setting takes when its environment variable is absent.
EXPECTED_DEFAULTS = {
    "LLM_REQUEST_TIMEOUT_SECONDS": 60.0,
    "LLM_RATE_LIMIT_BACKOFF_BASE_SECONDS": 2.0,
    "LLM_RATE_LIMIT_BACKOFF_MAX_SECONDS": 120.0,
    "LLM_RATE_LIMIT_HEADER_CEILING_SECONDS": 1800.0,
    "LLM_RATE_LIMIT_MAX_WAIT_SECONDS": 300.0,
    "LLM_RATE_LIMIT_INTERACTIVE_MAX_WAIT_SECONDS": 20.0,
    "LLM_TRANSIENT_RETRY_ATTEMPTS": 2,
    "LLM_TRANSIENT_RETRY_BASE_SECONDS": 1.0,
}


@pytest.mark.parametrize(("name", "expected"), EXPECTED_DEFAULTS.items())
def test_llm_settings_have_expected_defaults(name: str, expected):
    # Only the fallback is under test. A value configured for the environment the tests
    # run in is a deployment choice, not a regression. A blank counts as unset, which is
    # what an `.env` line with nothing after the '=' produces.
    if os.environ.get(name, "").strip():
        pytest.skip(f"{name} is set in the environment, so its default is not in play")

    assert getattr(dj_settings, name) == expected


# An `.env` line with nothing after the '=' means "not configured", so a blank has to fall
# through to the default rather than being parsed.
@pytest.mark.parametrize("raw", ["", "   "])
def test_optional_env_treats_blank_as_unset(monkeypatch, raw: str):
    monkeypatch.setenv("SOME_OPTIONAL_SETTING", raw)
    default = {"the": "default"}

    assert _optional_env("SOME_OPTIONAL_SETTING", json.loads, default) == default


def test_optional_env_falls_back_when_missing(monkeypatch):
    monkeypatch.delenv("SOME_OPTIONAL_SETTING", raising=False)

    assert _optional_env("SOME_OPTIONAL_SETTING", float, 60.0) == 60.0


def test_optional_env_parses_a_configured_value(monkeypatch):
    monkeypatch.setenv("SOME_OPTIONAL_SETTING", '{"reasoning_effort": "none"}')

    parsed = _optional_env("SOME_OPTIONAL_SETTING", json.loads, {})

    assert parsed == {"reasoning_effort": "none"}


@pytest.fixture
def clean_llm_env(monkeypatch):
    """Start from no LLM model configuration at all."""
    monkeypatch.delenv("LLM_DEFAULT_MODEL", raising=False)
    for feature in LLM_FEATURES:
        monkeypatch.delenv(f"LLM_{feature.upper()}_MODEL", raising=False)
    return monkeypatch


def test_every_feature_falls_back_to_the_default_model(clean_llm_env):
    clean_llm_env.setenv("LLM_DEFAULT_MODEL", "default-model?reasoning_effort=none")

    models = _resolve_llm_models()

    assert set(models) == set(LLM_FEATURES)
    for feature in LLM_FEATURES:
        assert models[feature].model == "default-model"
        assert models[feature].params == {"reasoning_effort": "none"}


def test_a_feature_can_override_the_default_model(clean_llm_env):
    clean_llm_env.setenv("LLM_DEFAULT_MODEL", "small-model")
    clean_llm_env.setenv("LLM_LABELING_MODEL", "big-model?reasoning_effort=low")

    models = _resolve_llm_models()

    assert models["labeling"].model == "big-model"
    assert models["labeling"].params == {"reasoning_effort": "low"}
    # Everything else keeps the default.
    assert models["chats"].model == "small-model"


def test_a_blank_override_falls_back_rather_than_failing(clean_llm_env):
    # The dev compose file passes every override through, empty when it is not configured.
    clean_llm_env.setenv("LLM_DEFAULT_MODEL", "default-model")
    clean_llm_env.setenv("LLM_LABELING_MODEL", "")

    assert _resolve_llm_models()["labeling"].model == "default-model"


def test_a_malformed_default_is_reported_as_a_configuration_error(clean_llm_env):
    clean_llm_env.setenv("LLM_DEFAULT_MODEL", "?reasoning_effort=none")

    with pytest.raises(ImproperlyConfigured, match="LLM_DEFAULT_MODEL"):
        _resolve_llm_models()


def test_a_malformed_override_names_the_setting_at_fault(clean_llm_env):
    clean_llm_env.setenv("LLM_DEFAULT_MODEL", "default-model")
    clean_llm_env.setenv("LLM_EXTRACTIONS_MODEL", "m?a=1&a.b=2")

    with pytest.raises(ImproperlyConfigured, match="LLM_EXTRACTIONS_MODEL"):
        _resolve_llm_models()


def test_a_valueless_parameter_is_a_boot_error(clean_llm_env):
    # Otherwise it boots fine and the provider 400s on every single request.
    clean_llm_env.setenv("LLM_DEFAULT_MODEL", "a-model?reasoning_effort=")

    with pytest.raises(ImproperlyConfigured, match="LLM_DEFAULT_MODEL"):
        _resolve_llm_models()


def test_a_malformed_optional_setting_names_the_variable(monkeypatch):
    # A bare "could not convert string to float" would leave the admin guessing.
    monkeypatch.setenv("SOME_OPTIONAL_SETTING", "60s")

    with pytest.raises(ImproperlyConfigured, match="SOME_OPTIONAL_SETTING"):
        _optional_env("SOME_OPTIONAL_SETTING", float, 60.0)
