"""Tests for the Django system check that guards EMBEDDING_DIM/migration parity."""

from django.test import override_settings

from radis.pgsearch.apps import (
    EMBEDDING_DIM_MIGRATION_LITERAL,
    check_embedding_dim_matches_migration,
)


def test_check_passes_when_dim_matches_migration():
    with override_settings(EMBEDDING_DIM=EMBEDDING_DIM_MIGRATION_LITERAL):
        assert check_embedding_dim_matches_migration(app_configs=None) == []


def test_check_fails_when_dim_diverges_from_migration():
    with override_settings(EMBEDDING_DIM=EMBEDDING_DIM_MIGRATION_LITERAL + 1):
        errors = check_embedding_dim_matches_migration(app_configs=None)
    assert len(errors) == 1
    err = errors[0]
    assert err.id == "pgsearch.E001"
    assert str(EMBEDDING_DIM_MIGRATION_LITERAL) in err.msg
    assert str(EMBEDDING_DIM_MIGRATION_LITERAL + 1) in err.msg
