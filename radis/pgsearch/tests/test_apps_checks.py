"""Tests for the Django system check that guards EMBEDDING_DIM/migration parity."""

from unittest.mock import patch

from django.test import override_settings

from radis.pgsearch.apps import (
    _migration_embedding_dim,
    check_embedding_dim_matches_migration,
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
