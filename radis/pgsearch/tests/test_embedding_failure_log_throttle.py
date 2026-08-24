"""Tests for the permanent-failure log throttle in `_embed_query_or_none`.

Embedding failures are deliberately not cached (see `_embed_query_cached`'s
docstring), so without a throttle a persistent misconfiguration (e.g.
EMBEDDINGS_MODEL pointed at an endpoint that serves no /v1/embeddings) would log a
full traceback on every single search request, indefinitely. These tests pin the
throttle: the first failure for a configuration logs a full traceback, a repeat
failure under the same configuration logs a single WARNING with no traceback, and a
failure under a changed configuration (a different endpoint or model) logs a fresh
traceback of its own.
"""

import logging
from unittest.mock import patch

import pytest

from radis.core.utils.embedding_client import EmbeddingClientError
from radis.core.utils.model_spec import parse_model_spec
from radis.pgsearch import providers

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _embeddings_configured(settings):
    settings.EMBEDDINGS_MODEL = parse_model_spec("qwen3")
    settings.EMBEDDINGS_BASE_URL = "http://gateway.example/v1"


# The process-global suppression set (providers._LOGGED_PERMANENT_FAILURE_CONFIGS) is
# reset by the autouse `_clear_logged_permanent_failure_configs` fixture in
# radis/pgsearch/tests/conftest.py, which every test in this package picks up
# automatically; no local reset fixture is needed here.


def _patch_embedding_client_to_fail():
    patcher = patch("radis.pgsearch.providers.EmbeddingClient")
    MockClient = patcher.start()
    MockClient.return_value.__enter__.return_value = MockClient.return_value
    MockClient.return_value.__exit__.return_value = None
    MockClient.return_value.embed_query.side_effect = EmbeddingClientError(
        "permanent: no /v1/embeddings on this endpoint"
    )
    return patcher


def test_first_permanent_failure_logs_full_traceback(caplog):
    patcher = _patch_embedding_client_to_fail()
    try:
        with caplog.at_level(logging.DEBUG, logger="radis.pgsearch.providers"):
            result = providers._embed_query_or_none("pneumonia", "test")
    finally:
        patcher.stop()

    assert result is None
    exception_records = [r for r in caplog.records if r.exc_info is not None]
    assert len(exception_records) == 1
    assert exception_records[0].levelname == "ERROR"


def test_second_identical_failure_logs_warning_without_traceback(caplog):
    patcher = _patch_embedding_client_to_fail()
    try:
        with caplog.at_level(logging.DEBUG, logger="radis.pgsearch.providers"):
            providers._embed_query_or_none("pneumonia", "test")
            caplog.clear()
            result = providers._embed_query_or_none("pleural effusion", "test")
    finally:
        patcher.stop()

    assert result is None
    assert all(r.exc_info is None for r in caplog.records)
    warning_records = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warning_records) == 1
    message = warning_records[0].getMessage()
    # Actionable on its own: names the settings to check, states the cost, names the
    # concrete cause (so a later different failure under the same config is
    # distinguishable from this one once its own traceback has scrolled out of recent
    # logs), and says the traceback was already logged once.
    assert "EMBEDDINGS_BASE_URL" in message
    assert "EMBEDDINGS_MODEL" in message
    assert "full-text-only" in message
    assert "once" in message
    assert "EmbeddingClientError" in message


def test_repeat_failure_then_changed_configuration(caplog, settings):
    """Chained so this test is self-sufficient evidence of the full throttle
    lifecycle, rather than relying on a sibling test for the "same config" half:
    fail under config A (full traceback) -> fail again under A (suppressed to a
    WARNING) -> switch to config B -> fail under B (a fresh traceback of its own)."""
    patcher = _patch_embedding_client_to_fail()
    try:
        with caplog.at_level(logging.DEBUG, logger="radis.pgsearch.providers"):
            providers._embed_query_or_none("pneumonia", "test")
            first_exceptions = [r for r in caplog.records if r.exc_info is not None]
            assert len(first_exceptions) == 1

            caplog.clear()
            providers._embed_query_or_none("pneumonia", "test")
            assert all(r.exc_info is None for r in caplog.records)
            assert any(r.levelname == "WARNING" for r in caplog.records)

            caplog.clear()
            settings.EMBEDDINGS_BASE_URL = "http://other-gateway.example/v1"
            result = providers._embed_query_or_none("pneumonia", "test")
    finally:
        patcher.stop()

    assert result is None
    exception_records = [r for r in caplog.records if r.exc_info is not None]
    assert len(exception_records) == 1
    assert exception_records[0].levelname == "ERROR"
