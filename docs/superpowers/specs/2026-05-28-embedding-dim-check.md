# EMBEDDING_DIM System Check — Single Source of Truth via MigrationLoader

**Status:** Draft — design phase
**Author:** RADIS team (Samuel Kwong)
**Date:** 2026-05-28
**Implementation skill (next step):** `writing-plans`
**Related:** `docs/superpowers/specs/2026-05-28-hybrid-search.md` §4.5 (operational procedure for `EMBEDDING_DIM` changes)

---

## 1. Problem

`radis/pgsearch/apps.py` currently maintains a hand-edited constant:

```python
# Keep in sync with the dimensions= literal in
# radis/pgsearch/migrations/0003_report_embedding.py.
EMBEDDING_DIM_MIGRATION_LITERAL = 1024
```

The Django system check `pgsearch.E001` compares this against `settings.EMBEDDING_DIM`
to catch the case where an operator changes the env var without running
`makemigrations`. Without the check, the divergence would surface later as an
opaque pgvector dimension error on the first write or query.

The constant has three failure modes:

- **Drift on migration changes.** If a new migration drops/recreates the
  embedding column at a different dim, `EMBEDDING_DIM_MIGRATION_LITERAL` must
  also be edited. Easy to forget.
- **Triple duplication.** The dim now lives in three places:
  `settings.EMBEDDING_DIM` (env), the migration literal, and the constant.
- **Wrong-by-construction risk.** The constant is the only one of the three
  that is *not* auto-derived from anything; the migration literal is generated
  by `makemigrations` from `settings.EMBEDDING_DIM` at generation time. The
  constant has to be transcribed by hand.

## 2. Goals & non-goals

### Goals

- Eliminate the hand-edited `EMBEDDING_DIM_MIGRATION_LITERAL` constant.
- Preserve the existing safety net: `manage.py check` must still fail
  loudly when `settings.EMBEDDING_DIM` diverges from what the migrations
  describe.
- Keep the check offline (no database connection required at startup).

### Non-goals

- Eliminating the §4.5 manual operator procedure (drop column, re-migrate,
  re-embed). That decoupling — non-disruptive dim changes via side-by-side
  columns or similar — is explicitly out of scope and is a future spec.
- Changing the on-disk migration format or the way `makemigrations` captures
  the literal `dimensions=1024`.
- Changing `settings.EMBEDDING_DIM` (still an env var).

## 3. Design

Use Django's `MigrationLoader` to compute the project state from the on-disk
migration files at check time, then read the embedding field's `dimensions`
from that state. The state is built without a database connection.

`radis/pgsearch/apps.py` becomes:

```python
from django.apps import AppConfig
from django.conf import settings
from django.core.checks import Error, register


class PgSearchConfig(AppConfig):
    name = "radis.pgsearch"

    def ready(self):
        from . import signals as signals  # noqa: F401

        register_app()


def _migration_embedding_dim() -> int | None:
    """Return the `dimensions` value of `ReportSearchVector.embedding` as
    captured by the on-disk pgsearch migrations. Returns None if the field
    cannot be located (e.g., migrations are missing or out of sync)."""
    from django.db.migrations.loader import MigrationLoader

    loader = MigrationLoader(connection=None, ignore_no_migrations=True)
    state = loader.project_state()
    try:
        model = state.apps.get_model("pgsearch", "ReportSearchVector")
        return model._meta.get_field("embedding").dimensions
    except (LookupError, AttributeError):
        return None


@register()
def check_embedding_dim_matches_migration(app_configs, **kwargs):
    """Fail loudly when settings.EMBEDDING_DIM diverges from the dim baked
    into the pgsearch migrations. Mismatched values would otherwise surface as
    opaque pgvector dimension errors on the first write/query."""
    migration_dim = _migration_embedding_dim()

    if migration_dim is None:
        return [
            Error(
                "Could not determine the embedding column dimension from the "
                "pgsearch migrations. Either the migrations are missing the "
                "embedding field or the model has been renamed.",
                id="pgsearch.E002",
                hint=(
                    "Verify that `radis/pgsearch/migrations/` contains a "
                    "migration that adds the `embedding` field to "
                    "`ReportSearchVector`, and that `makemigrations pgsearch` "
                    "succeeds without changes."
                ),
            )
        ]

    if settings.EMBEDDING_DIM != migration_dim:
        return [
            Error(
                f"EMBEDDING_DIM={settings.EMBEDDING_DIM} does not match the "
                f"dim baked into the pgsearch migrations "
                f"(vector({migration_dim})). Writes will fail with a pgvector "
                f"dimension error. Either set EMBEDDING_DIM={migration_dim}, or "
                f"run `makemigrations pgsearch` to capture the new dim and "
                f"follow the §4.5 procedure to drop and recreate the embedding "
                f"column.",
                id="pgsearch.E001",
                hint=(
                    "Update EMBEDDING_DIM in your .env to match the existing "
                    "migrations, or generate a new migration that matches the "
                    "new dim."
                ),
            )
        ]
    return []
```

`register_app()` is unchanged.

### 3.1 Why `MigrationLoader` and not other options

| Option | Authoritative for | DB connection | Verdict |
|---|---|---|---|
| Hand-edited constant (status quo) | Nothing — must be manually transcribed | No | Drift-prone |
| Parse `migrations/0003_*.py` source | The literal in one specific file | No | Brittle; couples to filename |
| `MigrationLoader` project state | The *aggregated* dim across all migrations | No | Chosen |
| `information_schema.columns` (live DB) | The actually-deployed column dim | Yes | Loses offline-check property |

`MigrationLoader` answers "what dim do the migrations on disk currently describe?"
which is exactly what the check needs to catch env/migration drift before any
DB writes happen. If a later migration drops and recreates the column at a
different dim, `project_state()` reflects the *post-all-migrations* state, so
the check stays correct without code changes.

### 3.2 Failure-mode coverage

| Scenario | Behavior |
|---|---|
| Env says 2560, migrations describe 1024 | `pgsearch.E001` fires with both numbers and the suggested fix. |
| Env says 1024, migrations also describe 1024 | Check passes. |
| `embedding` field deleted by a migration (no replacement) | `_migration_embedding_dim()` returns `None`; `pgsearch.E002` fires telling the operator to re-add it. |
| Fresh checkout, no migrations applied yet | `project_state()` still resolves from disk; check works without DB. |
| Migrations dir present but missing the embedding field somehow | `pgsearch.E002` (same path as deletion). |

## 4. What this does not change

- `settings.EMBEDDING_DIM` env var — still the runtime/code-facing value.
- The migration file `0003_report_embedding.py` — `dimensions=1024` literal
  stays as generated by `makemigrations`.
- The §4.5 manual operator procedure for changing the dim — still:
  edit env → makemigrations → drop column → re-migrate → defer launcher.
- The check's id (`pgsearch.E001`) — preserved so existing operator playbooks
  and any test that asserts on the id keep working. A new id `pgsearch.E002`
  is added for the "missing field" case.

## 5. Migration plan (code change, not Django migration)

One PR / small commit series:

1. **Delete `EMBEDDING_DIM_MIGRATION_LITERAL`** from `radis/pgsearch/apps.py`.
2. **Add `_migration_embedding_dim()` helper** to the same file.
3. **Rewrite `check_embedding_dim_matches_migration`** to use the helper and
   emit `pgsearch.E001` (dim mismatch) or `pgsearch.E002` (field missing).
4. **Tests** in `radis/pgsearch/tests/test_apps_checks.py`:
   - The existing two tests currently import `EMBEDDING_DIM_MIGRATION_LITERAL`
     from `apps.py`. That import goes away. Rewrite both tests to source the
     migration dim from the new `_migration_embedding_dim()` helper instead:
     - `test_check_passes_when_dim_matches_migration`: override
       `EMBEDDING_DIM` to `_migration_embedding_dim()` and assert no errors.
     - `test_check_fails_when_dim_diverges_from_migration`: override to
       `_migration_embedding_dim() + 1`; assert one `pgsearch.E001` error and
       both numbers appear in the message.
   - Add a test that monkey-patches `_migration_embedding_dim` to return
     `None` and asserts a single `pgsearch.E002` error.
   - Add a test that calls `_migration_embedding_dim()` directly and asserts
     it returns the integer `1024` (current value), proving the loader path
     works without a DB connection. This test should not be in a
     `@pytest.mark.django_db` block.

## 6. Open questions deferred to writing-plans

- Whether to also delete the `# Keep in sync with ...` comment block that
  documented the old constant — yes, it goes with the constant.
- Whether `pgsearch.E002` deserves a unit test that *actually* removes the
  embedding migration vs. just mocks the helper return value. Mock is fine for
  v1; deleting on-disk migrations in a test is risky and brittle.
