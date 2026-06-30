"""Tests for the Django system check that guards EMBEDDING_DIM/migration parity."""

import logging
import os
from unittest.mock import MagicMock, patch

from django.test import override_settings

from radis.pgsearch.apps import (
    _migration_embedding_dim,
    check_embedding_dim_matches_migration,
    check_legacy_embedding_vars,
)


def test_migration_embedding_dim_returns_int_without_db():
    dim = _migration_embedding_dim()
    assert isinstance(dim, int)
    assert dim == 1024


def test_check_passes_when_dim_matches_migration():
    dim = _migration_embedding_dim()
    with override_settings(EMBEDDING_DIM=dim):
        assert check_embedding_dim_matches_migration(app_configs=None) == []


def test_check_fails_with_e001_when_dim_diverges_from_migration():
    dim = _migration_embedding_dim()
    assert dim is not None
    with override_settings(EMBEDDING_DIM=dim + 1):
        errors = check_embedding_dim_matches_migration(app_configs=None)
    assert len(errors) == 1
    err = errors[0]
    assert err.id == "pgsearch.E001"
    assert str(dim) in err.msg
    assert str(dim + 1) in err.msg


def test_check_fails_with_e002_when_migration_field_missing():
    with patch("radis.pgsearch.apps._migration_embedding_dim", return_value=None):
        errors = check_embedding_dim_matches_migration(app_configs=None)
    assert len(errors) == 1
    assert errors[0].id == "pgsearch.E002"


def test_stamina_on_retry_hook_includes_log_stamina_retry():
    """`PgSearchConfig.ready()` registers our embed-call WARNING hook
    so stamina retries surface in logs."""
    from stamina.instrumentation import get_on_retry_hooks

    from radis.pgsearch.tasks import _log_stamina_retry

    assert _log_stamina_retry in get_on_retry_hooks()


def test_index_reports_logs_info_with_mode_and_count(settings, caplog):
    from radis.pgsearch.apps import _index_reports

    apps_logger = logging.getLogger("radis.pgsearch.apps")
    apps_logger.addHandler(caplog.handler)
    caplog.set_level(logging.INFO, logger="radis.pgsearch.apps")
    try:
        settings.PGSEARCH_SYNC_INDEXING = False
        reports = [MagicMock(pk=1), MagicMock(pk=2), MagicMock(pk=3)]
        with patch("radis.pgsearch.tasks.enqueue_bulk_index_reports"):
            _index_reports(reports)
    finally:
        apps_logger.removeHandler(caplog.handler)

    info_msgs = [r.getMessage() for r in caplog.records if r.levelname == "INFO"]
    assert any(
        "pgsearch.index_reports: handler invoked; reports=3 mode=async" in m for m in info_msgs
    )


def test_legacy_embedding_backend_var_raises_e002():
    with patch.dict(os.environ, {"EMBEDDING_BACKEND": "openai"}, clear=False):
        errors = check_legacy_embedding_vars(app_configs=None)
    assert any(e.id == "pgsearch.E002" for e in errors)


def test_legacy_embedding_provider_path_var_raises_e003():
    with patch.dict(os.environ, {"EMBEDDING_PROVIDER_PATH": "/api/embeddings"}, clear=False):
        errors = check_legacy_embedding_vars(app_configs=None)
    assert any(e.id == "pgsearch.E003" for e in errors)


def test_no_errors_when_legacy_vars_absent(monkeypatch):
    monkeypatch.delenv("EMBEDDING_BACKEND", raising=False)
    monkeypatch.delenv("EMBEDDING_PROVIDER_PATH", raising=False)
    assert check_legacy_embedding_vars(app_configs=None) == []
