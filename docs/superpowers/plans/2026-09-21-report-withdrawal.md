# Report Withdrawal Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Admins can withdraw a report (reversible, audited soft delete) and restore it later; a withdrawn report is invisible everywhere outside the admin, including search.

**Architecture:** Three fields on `Report` carry the state (`withdrawn_at` doubles as the flag); an eleventh trigger-maintained projection column on `ReportSearchIndex` makes the search scan fail-closed for all four provider entry points; an explicit `.live()` queryset filter covers the handful of direct-read surfaces; the admin gets a reason-collecting withdraw action and a `WithdrawnReport` proxy page with a restore action.

**Tech Stack:** Django 6, PostgreSQL 17 (statement-level triggers with transition tables), DRF, pytest-django, factory-boy.

**Spec:** `docs/superpowers/specs/2026-09-21-report-withdrawal-design.md` — the plan argues from the spec; read both.

## Global Constraints

- Branch: all work happens on `report-withdrawal` (based on `fts-query-shape-performance`, PR #292). Never commit to either base branch.
- Naming: `withdraw` / `withdrawn` / `restore` in identifiers and UI copy — never "delete", "hide" or "soft delete".
- `Report.objects` stays an UNFILTERED default manager. Exclusion is always an explicit `.live()` / `withdrawn_at__isnull=True` at the call site (spec decision 4).
- Migrations: `AddField` only nullable or with a constant default (metadata-only on 8M rows); index creation only via `AddIndexConcurrently` in an `atomic = False` migration; never edit an already-committed migration file.
- Style: Ruff (line length 100, rules E, F, I, DJ) and djlint — `uv run cli lint` must pass at every commit. Pyright basic — keep annotations consistent with surrounding code.
- Comments explain *why*, never *what* or history ("was previously…" is forbidden — describe the code as it is).
- Tests: TDD — write the failing test first, watch it fail, implement, watch it pass. Run with `uv run pytest <path> -v` (needs the PostgreSQL from `.env` reachable; pytest-django creates a separate `test_…` database). Per-test timeout is 60 s.
- `makemigrations` is run as `uv run ./manage.py makemigrations <app> -n <name>` (no DB connection needed).
- Commit messages: sentence-case imperative subject, no `feat:`-style prefixes (repo style: "Document VACUUM FULL as the compaction procedure"), and every message ends with the two trailers:

  ```
  Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_01VDei6anDxfR5eoHhFfXBGs
  ```

## File Map

| File | Role in this plan |
| --- | --- |
| `radis/reports/models.py` | withdrawal fields, `ReportQuerySet.live()`, `ReportManager`, `is_withdrawn`, `WithdrawnReport` proxy |
| `radis/reports/migrations/0014…0016` | fields (metadata-only), partial index (concurrent), proxy (state-only) |
| `radis/pgsearch/models.py` + `migrations/0007, 0008` | `withdrawn` projection column + trigger function replacement |
| `radis/pgsearch/utils/projection.py`, `management/commands/check_search_projection.py` | the synced SQL copies gain the column |
| `radis/pgsearch/providers.py` | `Q(withdrawn=False)` in `_build_filter_query` |
| `radis/reports/views.py`, `api/serializers.py`, `api/viewsets.py`, `admin.py`, `templates/admin/reports/report/withdraw_selected_confirmation.html` | user surfaces, API semantics, admin actions |
| `radis/collections/views.py` + `utils/exporters.py`, `radis/notes/views.py`, `radis/chats/views.py`, `radis/subscriptions/views.py` + `processors.py`, `radis/extractions/processors.py` | the direct-read surfaces and worker re-checks |
| `docs/user-docs/admin-guide.md` | operator documentation |

Tests live beside what they test: `radis/reports/tests/test_models.py` (new), `test_views.py` (new), `test_admin.py` (new), `test_api.py` (extend); `radis/pgsearch/tests/test_search_projection.py`, `test_providers.py`, `test_check_search_projection.py` (extend); `radis/collections/tests/test_views.py`, `test_exporters.py` (extend); `radis/notes/tests/test_views.py` (extend); `radis/chats/tests/test_views.py` (extend); `radis/subscriptions/tests/test_views.py`, `test_processors.py` (extend); `radis/extractions/tests/test_processors.py` (extend).

---

### Task 1: Withdrawal fields and `.live()` on Report

**Files:**
- Modify: `radis/reports/models.py`
- Create: `radis/reports/migrations/0014_report_withdrawal_fields.py` (via makemigrations)
- Test: `radis/reports/tests/test_models.py` (new file)

**Interfaces:**
- Consumes: nothing new.
- Produces: `Report.withdrawn_at: datetime | None`, `Report.withdrawn_by: User | None`, `Report.withdrawal_reason: str`, `Report.is_withdrawn: bool` (property), `Report.objects.live() -> ReportQuerySet` (also available on every related manager, e.g. `collection.reports.live()`, `task.reports.live()`). Every later task relies on exactly these names.

- [ ] **Step 1: Write the failing tests**

Create `radis/reports/tests/test_models.py`:

```python
import pytest
from adit_radis_shared.accounts.factories import GroupFactory
from django.utils import timezone

from radis.reports.factories import LanguageFactory, ReportFactory
from radis.reports.models import Report

pytestmark = pytest.mark.django_db


def create_report() -> Report:
    return ReportFactory.create(language=LanguageFactory.create(code="en"))


def test_report_defaults_to_live():
    report = create_report()

    assert not report.is_withdrawn
    assert Report.objects.live().filter(pk=report.pk).exists()


def test_live_excludes_withdrawn_reports_but_default_manager_keeps_them():
    report = create_report()

    Report.objects.filter(pk=report.pk).update(withdrawn_at=timezone.now())

    report.refresh_from_db()
    assert report.is_withdrawn
    assert not Report.objects.live().filter(pk=report.pk).exists()
    assert Report.objects.filter(pk=report.pk).exists()


def test_live_is_available_on_related_managers():
    group = GroupFactory.create()
    live = create_report()
    withdrawn = create_report()
    live.groups.add(group)
    withdrawn.groups.add(group)

    Report.objects.filter(pk=withdrawn.pk).update(withdrawn_at=timezone.now())

    assert list(group.reports.live()) == [live]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest radis/reports/tests/test_models.py -v`
Expected: FAIL — `AttributeError: 'Manager' object has no attribute 'live'` / `Report` has no field `withdrawn_at`.

- [ ] **Step 3: Implement the model changes**

In `radis/reports/models.py`:

Add to the imports (top of file):

```python
from django.conf import settings
```

Insert directly above `class Report(models.Model):`:

```python
class ReportQuerySet(models.QuerySet["Report"]):
    def live(self) -> "ReportQuerySet":
        """Reports in circulation. Every user-facing read surface filters on
        this; the admin and ingest paths deliberately use the unfiltered
        default manager (see the withdrawal design spec, decision 4)."""
        return self.filter(withdrawn_at__isnull=True)


class ReportManager(models.Manager["Report"]):
    def get_queryset(self) -> ReportQuerySet:
        return ReportQuerySet(self.model, using=self._db)

    def live(self) -> ReportQuerySet:
        return self.get_queryset().live()
```

Inside `Report`, directly after the `updated_at = models.DateTimeField(auto_now=True)` line, add:

```python
    # Withdrawal takes a report out of circulation without deleting it.
    # withdrawn_at IS the state flag: NULL means live. who/why describe only
    # the current withdrawal; restoring blanks all three (the admin log keeps
    # the breadcrumbs).
    withdrawn_at = models.DateTimeField(null=True, blank=True)
    withdrawn_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    withdrawal_reason = models.TextField(blank=True, default="")

    objects = ReportManager()
```

Inside `Report`, after the `modality_codes` property, add:

```python
    @property
    def is_withdrawn(self) -> bool:
        return self.withdrawn_at is not None
```

- [ ] **Step 4: Generate the migration**

Run: `uv run ./manage.py makemigrations reports -n report_withdrawal_fields`
Expected: creates `radis/reports/migrations/0014_report_withdrawal_fields.py` containing exactly three `AddField` operations (`withdrawn_at`, `withdrawn_by`, `withdrawal_reason`). All three are nullable or constant-defaulted — metadata-only. If the file contains anything else (an `AlterField`, an index), stop and investigate before committing.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest radis/reports/tests/test_models.py -v`
Expected: 3 passed.

- [ ] **Step 6: Lint and commit**

```bash
uv run cli lint
git add radis/reports/models.py radis/reports/migrations/0014_report_withdrawal_fields.py radis/reports/tests/test_models.py
git commit -m "Add withdrawal fields and a live() queryset to Report"
```

---

### Task 2: Partial index for the withdrawn listing

**Files:**
- Modify: `radis/reports/models.py` (Report.Meta)
- Create: `radis/reports/migrations/0015_report_withdrawn_partial_index.py`

**Interfaces:**
- Consumes: `Report.withdrawn_at` (Task 1).
- Produces: index `reports_report_withdrawn_idx` — serves the admin's withdrawn-only listing (Task 14); contains only withdrawn rows, so live-report writes don't maintain it.

- [ ] **Step 1: Add the index to Report.Meta**

In `radis/reports/models.py`, replace

```python
    class Meta:
        ordering = ["-created_at", "document_id"]
```

with

```python
    class Meta:
        ordering = ["-created_at", "document_id"]
        indexes = [
            # Partial: only withdrawn rows enter it, so the withdrawn-reports
            # admin listing never scans the archive and live-report writes
            # don't pay for it.
            models.Index(
                fields=["withdrawn_at"],
                condition=models.Q(withdrawn_at__isnull=False),
                name="reports_report_withdrawn_idx",
            ),
        ]
```

- [ ] **Step 2: Write the migration by hand**

`makemigrations` would emit a transactional `AddIndex`; on a large live archive the build must be concurrent. Create `radis/reports/migrations/0015_report_withdrawn_partial_index.py` with exactly:

```python
from django.contrib.postgres.operations import AddIndexConcurrently
from django.db import migrations, models


class Migration(migrations.Migration):
    # CREATE INDEX CONCURRENTLY cannot run inside a transaction.
    atomic = False

    dependencies = [
        ("reports", "0014_report_withdrawal_fields"),
    ]

    operations = [
        AddIndexConcurrently(
            model_name="report",
            index=models.Index(
                fields=["withdrawn_at"],
                condition=models.Q(withdrawn_at__isnull=False),
                name="reports_report_withdrawn_idx",
            ),
        ),
    ]
```

- [ ] **Step 3: Verify state and SQL**

Run: `uv run ./manage.py makemigrations reports --check --dry-run`
Expected: "No changes detected in app 'reports'" (the hand-written migration fully covers the Meta change).

Run: `uv run ./manage.py sqlmigrate reports 0015`
Expected: a single `CREATE INDEX CONCURRENTLY "reports_report_withdrawn_idx" … WHERE "withdrawn_at" IS NOT NULL;`

- [ ] **Step 4: Run the model tests to verify migrations still apply cleanly**

Run: `uv run pytest radis/reports/tests/test_models.py -v`
Expected: 3 passed (pytest-django applies both new migrations when building the test database; a broken migration fails here).

- [ ] **Step 5: Lint and commit**

```bash
uv run cli lint
git add radis/reports/models.py radis/reports/migrations/0015_report_withdrawn_partial_index.py
git commit -m "Index withdrawn reports partially for the admin listing"
```

---

### Task 3: `withdrawn` projection column on ReportSearchIndex

**Files:**
- Modify: `radis/pgsearch/models.py`
- Create: `radis/pgsearch/migrations/0007_reportsearchindex_withdrawn.py` (via makemigrations)
- Test: `radis/pgsearch/tests/test_search_projection.py` (extend)

**Interfaces:**
- Consumes: nothing from earlier tasks (the column is independent of the reports fields until Task 4 wires them together).
- Produces: `ReportSearchIndex.withdrawn: bool` (NOT NULL, default `False`). Task 4's trigger writes it; Task 5's predicate reads it.

- [ ] **Step 1: Write the failing test**

Add to `radis/pgsearch/tests/test_search_projection.py` (after `test_new_index_row_defaults_to_empty_arrays`):

```python
def test_new_index_row_defaults_to_not_withdrawn():
    report = ReportFactory.create(language=LanguageFactory.create(code="en"))
    index = ReportSearchIndex.objects.get(report=report)

    assert index.withdrawn is False
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest radis/pgsearch/tests/test_search_projection.py::test_new_index_row_defaults_to_not_withdrawn -v`
Expected: FAIL — `AttributeError: 'ReportSearchIndex' object has no attribute 'withdrawn'`.

- [ ] **Step 3: Add the field**

In `radis/pgsearch/models.py`, directly after `report_updated_at = models.DateTimeField(null=True)`, add:

```python
    # Mirrors reports_report.withdrawn_at IS NOT NULL. A boolean, not a
    # timestamp: the scan predicate only needs live/not-live, and a constant
    # default keeps the AddField metadata-only.
    withdrawn = models.BooleanField(default=False)
```

In the projection block comment above `group_ids` (same file), replace the sentence

```
    # the FTS candidate query stays single-table. Maintained by the triggers in
    # migration 0004 and populated on creation by signals.py / indexing.py.
```

with

```
    # the FTS candidate query stays single-table. Maintained by the projection
    # triggers (declared in migration 0004; the report-fields function body
    # lives in 0008) and populated on creation by signals.py / indexing.py.
```

- [ ] **Step 4: Generate the migration and verify it is metadata-only**

Run: `uv run ./manage.py makemigrations pgsearch -n reportsearchindex_withdrawn`
Expected: `0007_reportsearchindex_withdrawn.py` with a single `AddField` (`BooleanField(default=False)`).

Run: `uv run ./manage.py sqlmigrate pgsearch 0007`
Expected: `ADD COLUMN "withdrawn" boolean DEFAULT false NOT NULL` followed by `ALTER COLUMN "withdrawn" DROP DEFAULT` — a constant default, so PostgreSQL applies it without rewriting the table. No backfill migration follows: no withdrawn report can exist before this release ships, so `false` is already correct for every row.

- [ ] **Step 5: Run the test to verify it passes**

Run: `uv run pytest radis/pgsearch/tests/test_search_projection.py::test_new_index_row_defaults_to_not_withdrawn -v`
Expected: PASS.

- [ ] **Step 6: Lint and commit**

```bash
uv run cli lint
git add radis/pgsearch/models.py radis/pgsearch/migrations/0007_reportsearchindex_withdrawn.py radis/pgsearch/tests/test_search_projection.py
git commit -m "Add the withdrawn projection column to ReportSearchIndex"
```

---

### Task 4: Trigger function 0008 and the synced SQL copies

**Files:**
- Create: `radis/pgsearch/migrations/0008_search_projection_withdrawn_trigger.py`
- Modify: `radis/pgsearch/utils/projection.py`
- Modify: `radis/pgsearch/management/commands/check_search_projection.py`
- Test: `radis/pgsearch/tests/test_search_projection.py`, `radis/pgsearch/tests/test_check_search_projection.py` (extend)

**Interfaces:**
- Consumes: `Report.withdrawn_at` (Task 1), `ReportSearchIndex.withdrawn` (Task 3).
- Produces: any UPDATE of `reports_report` keeps `withdrawn` in sync (this is what Tasks 5–14 rely on when they flip `withdrawn_at` with `queryset.update()`); `sync_projection()` and the drift check cover the new column.

- [ ] **Step 1: Write the failing tests**

Add to `radis/pgsearch/tests/test_search_projection.py`. Extend the imports with:

```python
from django.utils import timezone
```

Then add the tests:

```python
def test_withdrawing_a_report_updates_the_projection():
    report = ReportFactory.create(language=LanguageFactory.create(code="en"))

    Report.objects.filter(pk=report.pk).update(withdrawn_at=timezone.now())

    index = ReportSearchIndex.objects.get(report=report)
    assert index.withdrawn is True


def test_restoring_a_report_updates_the_projection():
    report = ReportFactory.create(language=LanguageFactory.create(code="en"))
    Report.objects.filter(pk=report.pk).update(withdrawn_at=timezone.now())

    Report.objects.filter(pk=report.pk).update(withdrawn_at=None)

    index = ReportSearchIndex.objects.get(report=report)
    assert index.withdrawn is False


def test_sync_projection_fills_withdrawn():
    """sync_projection must repair a corrupted withdrawn mirror, like it
    repairs every other projection column."""
    report = ReportFactory.create(language=LanguageFactory.create(code="en"))
    Report.objects.filter(pk=report.pk).update(withdrawn_at=timezone.now())
    ReportSearchIndex.objects.filter(report=report).update(withdrawn=False)

    sync_projection([report.pk])

    assert ReportSearchIndex.objects.get(report=report).withdrawn is True
```

Add to `radis/pgsearch/tests/test_check_search_projection.py` (match that file's existing imports; it already tests the command — extend them with whatever of `pytest`, `call_command`, `CommandError`, `timezone`, `ReportSearchIndex`, `Report`, `ReportFactory`, `LanguageFactory` is missing):

```python
def test_check_passes_after_withdraw_and_restore():
    report = ReportFactory.create(language=LanguageFactory.create(code="en"))

    Report.objects.filter(pk=report.pk).update(withdrawn_at=timezone.now())
    call_command("check_search_projection")

    Report.objects.filter(pk=report.pk).update(withdrawn_at=None)
    call_command("check_search_projection")


def test_check_detects_withdrawn_drift():
    report = ReportFactory.create(language=LanguageFactory.create(code="en"))
    ReportSearchIndex.objects.filter(report=report).update(withdrawn=True)

    with pytest.raises(CommandError, match="withdrawn"):
        call_command("check_search_projection")
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest radis/pgsearch/tests/test_search_projection.py -k withdraw -v` and `uv run pytest radis/pgsearch/tests/test_check_search_projection.py -k withdraw -v`
Expected: the trigger tests fail with `withdrawn is False` after the update (the 0004 function body doesn't know the column); `test_check_detects_withdrawn_drift` fails because the drift query doesn't check the column yet.

- [ ] **Step 3: Write migration 0008**

Create `radis/pgsearch/migrations/0008_search_projection_withdrawn_trigger.py` with exactly:

```python
"""Extend the report-fields projection trigger with the withdrawn flag.

Only the function body changes. The trigger declaration from 0004
(pgsearch_report_fields_upd, AFTER UPDATE ON reports_report, statement level
with a transition table, no column list) already fires on every report update,
so replacing the function is the whole change. The reverse operation restores
the 0004 body, leaving a rollback exactly as 0004 defined it.
"""

from django.db import migrations

# Same column list as utils/projection.PROJECTION_UPDATE_SQL and
# check_search_projection.DRIFT_SQL -- keep them in sync. The drift query is
# the detector: a change made here and not there is a bug that reports itself
# as healthy.
SYNC_REPORT_FIELDS_WITH_WITHDRAWN = """
CREATE OR REPLACE FUNCTION pgsearch_sync_report_fields() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    UPDATE pgsearch_reportsearchindex rsi
    SET language_code = l.code,
        patient_sex = c.patient_sex,
        patient_age = c.patient_age,
        patient_id = c.patient_id,
        study_datetime = c.study_datetime,
        study_description = c.study_description,
        report_created_at = c.created_at,
        report_updated_at = c.updated_at,
        withdrawn = (c.withdrawn_at IS NOT NULL)
    FROM changed c
    LEFT JOIN reports_language l ON l.id = c.language_id
    WHERE rsi.report_id = c.id
      AND (rsi.language_code, rsi.patient_sex, rsi.patient_age, rsi.patient_id,
           rsi.study_datetime, rsi.study_description, rsi.report_created_at,
           rsi.report_updated_at, rsi.withdrawn)
          IS DISTINCT FROM
          (l.code, c.patient_sex, c.patient_age, c.patient_id,
           c.study_datetime, c.study_description, c.created_at, c.updated_at,
           (c.withdrawn_at IS NOT NULL));
    RETURN NULL;
END $$;
"""

SYNC_REPORT_FIELDS_WITHOUT_WITHDRAWN = """
CREATE OR REPLACE FUNCTION pgsearch_sync_report_fields() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    UPDATE pgsearch_reportsearchindex rsi
    SET language_code = l.code,
        patient_sex = c.patient_sex,
        patient_age = c.patient_age,
        patient_id = c.patient_id,
        study_datetime = c.study_datetime,
        study_description = c.study_description,
        report_created_at = c.created_at,
        report_updated_at = c.updated_at
    FROM changed c
    LEFT JOIN reports_language l ON l.id = c.language_id
    WHERE rsi.report_id = c.id
      AND (rsi.language_code, rsi.patient_sex, rsi.patient_age, rsi.patient_id,
           rsi.study_datetime, rsi.study_description, rsi.report_created_at,
           rsi.report_updated_at)
          IS DISTINCT FROM
          (l.code, c.patient_sex, c.patient_age, c.patient_id,
           c.study_datetime, c.study_description, c.created_at, c.updated_at);
    RETURN NULL;
END $$;
"""


class Migration(migrations.Migration):
    dependencies = [
        ("pgsearch", "0007_reportsearchindex_withdrawn"),
        ("reports", "0014_report_withdrawal_fields"),
    ]

    operations = [
        migrations.RunSQL(
            SYNC_REPORT_FIELDS_WITH_WITHDRAWN,
            SYNC_REPORT_FIELDS_WITHOUT_WITHDRAWN,
        ),
    ]
```

- [ ] **Step 4: Extend PROJECTION_UPDATE_SQL**

In `radis/pgsearch/utils/projection.py`, in `PROJECTION_UPDATE_SQL`, replace the line

```
    report_updated_at = r.updated_at
```

with

```
    report_updated_at = r.updated_at,
    withdrawn = (r.withdrawn_at IS NOT NULL)
```

In the module docstring of the same file, replace the sentence

```
The two trigger functions in migration 0004 and check_search_projection.DRIFT_SQL repeat the
same array_agg shape and belong to that set as well.
```

with

```
The two array_agg trigger functions in migration 0004 and
check_search_projection.DRIFT_SQL repeat the same aggregation shape and belong
to that set as well; the live body of pgsearch_sync_report_fields() is defined
in migration 0008 and mirrors the scalar columns plus the withdrawn flag.
```

(If the sentence wraps differently in the file, replace the whole sentence wherever it starts — the content is what matters.)

- [ ] **Step 5: Extend DRIFT_SQL**

In `radis/pgsearch/management/commands/check_search_projection.py`, in `DRIFT_SQL`, replace

```
    count(*) FILTER (WHERE rsi.report_updated_at IS DISTINCT FROM r.updated_at)
        AS report_updated_at
FROM pgsearch_reportsearchindex rsi
```

with

```
    count(*) FILTER (WHERE rsi.report_updated_at IS DISTINCT FROM r.updated_at)
        AS report_updated_at,
    count(*) FILTER (WHERE rsi.withdrawn IS DISTINCT FROM (r.withdrawn_at IS NOT NULL))
        AS withdrawn
FROM pgsearch_reportsearchindex rsi
```

In the comment above `DRIFT_SQL`, replace `the two trigger functions in migration 0004` with `the trigger functions in migrations 0004 and 0008` (keep the rest of the comment).

- [ ] **Step 6: Run the tests to verify they pass**

Run: `uv run pytest radis/pgsearch/tests/test_search_projection.py radis/pgsearch/tests/test_check_search_projection.py -v`
Expected: all pass, including the pre-existing tests (the trigger replacement must not break the other columns).

- [ ] **Step 7: Lint and commit**

```bash
uv run cli lint
git add radis/pgsearch/migrations/0008_search_projection_withdrawn_trigger.py radis/pgsearch/utils/projection.py radis/pgsearch/management/commands/check_search_projection.py radis/pgsearch/tests/
git commit -m "Sync the withdrawn flag into the search projection"
```

---

### Task 5: Fail-closed search predicate

**Files:**
- Modify: `radis/pgsearch/providers.py` (`_build_filter_query`, around line 194)
- Test: `radis/pgsearch/tests/test_providers.py` (extend)

**Interfaces:**
- Consumes: `ReportSearchIndex.withdrawn` kept in sync by the Task 4 trigger.
- Produces: all four provider entry points — `search()`, `count()`, `retrieve()`, `filter()` — exclude withdrawn reports. This is the guarantee interactive search, extractions and subscription refreshes get for free; later tasks don't re-filter those paths.

- [ ] **Step 1: Write the failing tests**

Add to `radis/pgsearch/tests/test_providers.py` (the module already imports `providers`, `Report`, `SearchFilters`, `timezone`, and defines `make_report`, `run_search`, `parse`, `_search`, `_search_group`):

```python
# ---------------------------------------------------------------------------
# Withdrawn reports are excluded from every provider entry point.
# ---------------------------------------------------------------------------


def test_search_excludes_withdrawn_reports():
    live = make_report("acute pneumothorax on the left")
    withdrawn = make_report("chronic pneumothorax on the right")
    Report.objects.filter(pk=withdrawn.pk).update(withdrawn_at=timezone.now())

    assert run_search("pneumothorax") == [live.document_id]


def test_count_excludes_withdrawn_reports():
    make_report("pneumothorax after biopsy")
    withdrawn = make_report("tension pneumothorax")
    Report.objects.filter(pk=withdrawn.pk).update(withdrawn_at=timezone.now())

    node = parse("pneumothorax")
    assert node is not None
    assert providers.count(_search(node)) == 1


def test_retrieve_excludes_withdrawn_reports():
    live = make_report("small apical pneumothorax")
    withdrawn = make_report("resolving pneumothorax")
    Report.objects.filter(pk=withdrawn.pk).update(withdrawn_at=timezone.now())

    node = parse("pneumothorax")
    assert node is not None
    assert list(providers.retrieve(_search(node))) == [live.document_id]


def test_filter_excludes_withdrawn_reports():
    live = make_report("unremarkable follow-up examination")
    withdrawn = make_report("unremarkable baseline examination")
    Report.objects.filter(pk=withdrawn.pk).update(withdrawn_at=timezone.now())

    document_ids = set(providers.filter(SearchFilters(group=_search_group().pk)))
    assert live.document_id in document_ids
    assert withdrawn.document_id not in document_ids
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest radis/pgsearch/tests/test_providers.py -k withdrawn -v`
Expected: 4 FAILED — the withdrawn report still appears in every result.

- [ ] **Step 3: Add the predicate**

In `radis/pgsearch/providers.py`, in `_build_filter_query`, directly after the `group` block (after `fq = Q(group_ids__contains=[filters.group])` / its `else`), insert:

```python
    # Withdrawn reports are out of circulation (reports.Report.withdrawn_at).
    # Excluding them here makes every provider entry point -- search(),
    # count(), retrieve() and filter() -- fail-closed at once.
    fq &= Q(withdrawn=False)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest radis/pgsearch/tests/test_providers.py -v`
Expected: all pass (the new four plus every pre-existing provider test).

- [ ] **Step 5: Lint and commit**

```bash
uv run cli lint
git add radis/pgsearch/providers.py radis/pgsearch/tests/test_providers.py
git commit -m "Exclude withdrawn reports from every search provider entry point"
```

---

### Task 6: API semantics — read-only state, upsert stays withdrawn

**Files:**
- Modify: `radis/reports/api/serializers.py` (`ReportSerializer`)
- Modify: `radis/reports/api/viewsets.py` (`bulk_upsert` response)
- Test: `radis/reports/tests/test_api.py` (extend)

**Interfaces:**
- Consumes: `Report.is_withdrawn`, `Report.withdrawn_at` (Task 1).
- Produces: every report API response carries read-only `withdrawn: bool` and `withdrawn_at`; `withdrawn_at`/`withdrawn_by`/`withdrawal_reason` are not writable through any serializer path; the bulk-upsert response always contains `"withdrawn": [document_id, …]` (sorted, possibly empty). Spec decision 1 falls out of the serializer contract: an upsert can never change withdrawal state.

- [ ] **Step 1: Write the failing tests**

Add to `radis/reports/tests/test_api.py`. Extend the imports with (only what is missing):

```python
from adit_radis_shared.accounts.factories import GroupFactory
from django.utils import timezone
```

Then add:

```python
# ---------------------------------------------------------------------------
# Withdrawal state on the API (read-only; upsert never restores)
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_retrieve_shows_withdrawal_state(admin_client):
    group = GroupFactory.create()
    admin_client.post(
        LIST_URL, make_payload(document_id="doc-withdrawn-get", group=group), format="json"
    )
    Report.objects.filter(document_id="doc-withdrawn-get").update(withdrawn_at=timezone.now())

    response = admin_client.get(detail_url("doc-withdrawn-get"))

    assert response.status_code == 200
    body = response.json()
    assert body["withdrawn"] is True
    assert body["withdrawn_at"] is not None


@pytest.mark.django_db
def test_upsert_put_on_withdrawn_report_updates_content_but_stays_withdrawn(admin_client):
    group = GroupFactory.create()
    create_response = admin_client.post(
        LIST_URL, make_payload(document_id="doc-withdrawn-upsert", group=group), format="json"
    )
    assert create_response.status_code == 201
    Report.objects.filter(document_id="doc-withdrawn-upsert").update(
        withdrawn_at=timezone.now(), withdrawal_reason="wrong patient"
    )

    payload = make_payload(document_id="doc-withdrawn-upsert", group=group)
    payload["body"] = "Corrected findings"
    # A client trying to clear the state must be ignored (read-only fields).
    payload["withdrawn_at"] = None
    payload["withdrawal_reason"] = ""
    response = admin_client.put(
        detail_url("doc-withdrawn-upsert") + "?upsert=true", payload, format="json"
    )

    assert response.status_code == 200
    assert response.json()["withdrawn"] is True
    report = Report.objects.get(document_id="doc-withdrawn-upsert")
    assert report.body == "Corrected findings"
    assert report.is_withdrawn
    assert report.withdrawal_reason == "wrong patient"


@pytest.mark.django_db
def test_bulk_upsert_reports_withdrawn_document_ids(admin_client):
    group = GroupFactory.create()
    admin_client.post(
        LIST_URL, make_payload(document_id="doc-bulk-live", group=group), format="json"
    )
    admin_client.post(
        LIST_URL, make_payload(document_id="doc-bulk-withdrawn", group=group), format="json"
    )
    Report.objects.filter(document_id="doc-bulk-withdrawn").update(withdrawn_at=timezone.now())

    payloads = [
        make_payload(document_id="doc-bulk-live", group=group),
        make_payload(document_id="doc-bulk-withdrawn", group=group),
        make_payload(document_id="doc-bulk-new", group=group),
    ]
    response = admin_client.post(BULK_UPSERT_URL, payloads, format="json")

    assert response.status_code == 200
    body = response.json()
    assert body["created"] == 1
    assert body["updated"] == 2
    assert body["withdrawn"] == ["doc-bulk-withdrawn"]
    assert Report.objects.get(document_id="doc-bulk-withdrawn").is_withdrawn
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest radis/reports/tests/test_api.py -k withdrawn -v`
Expected: FAIL — `KeyError: 'withdrawn'` on the responses (and the bulk response has no `withdrawn` key).

- [ ] **Step 3: Extend the serializer**

In `radis/reports/api/serializers.py`, in `ReportSerializer`, add a declared field after `modalities = ModalitySerializer(many=True)`:

```python
    withdrawn = serializers.BooleanField(source="is_withdrawn", read_only=True)
```

and extend its `Meta`:

```python
    class Meta:
        model = Report
        fields = "__all__"
        # Withdrawal is an admin-page action; no API write can set or clear it
        # (spec decision 1: an upsert updates content but never restores).
        read_only_fields = ("withdrawn_at", "withdrawn_by", "withdrawal_reason")
```

- [ ] **Step 4: Extend the bulk-upsert response**

In `radis/reports/api/viewsets.py`, in `bulk_upsert`, replace

```python
        response_body: dict[str, Any] = {
            "created": len(created_ids),
            "updated": len(updated_ids),
            "invalid": len(errors),
        }
```

with

```python
        withdrawn_ids: list[str] = []
        if updated_ids:
            withdrawn_ids = sorted(
                Report.objects.filter(
                    document_id__in=updated_ids, withdrawn_at__isnull=False
                ).values_list("document_id", flat=True)
            )

        response_body: dict[str, Any] = {
            "created": len(created_ids),
            "updated": len(updated_ids),
            "invalid": len(errors),
            # Ingest pipelines watch this: these reports were updated but stay
            # out of circulation until an admin restores them.
            "withdrawn": withdrawn_ids,
        }
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest radis/reports/tests/test_api.py radis/reports/tests/test_bulk_upsert.py radis/reports/tests/test_serializers.py -v`
Expected: all pass — the new three plus every pre-existing API/serializer test (the `read_only_fields` addition must not break create/update flows).

- [ ] **Step 6: Lint and commit**

```bash
uv run cli lint
git add radis/reports/api/serializers.py radis/reports/api/viewsets.py radis/reports/tests/test_api.py
git commit -m "Expose withdrawal state read-only on the reports API"
```

---

### Task 7: Reports views hide withdrawn reports

**Files:**
- Modify: `radis/reports/views.py`
- Test: `radis/reports/tests/test_views.py` (new file)

**Interfaces:**
- Consumes: `Report.objects.live()` (Task 1).
- Produces: `report_list` hides withdrawn reports; `report_detail` and `report_body` return 404 for them. Nothing downstream consumes code from this task.

- [ ] **Step 1: Write the failing tests**

Create `radis/reports/tests/test_views.py`:

```python
import pytest
from adit_radis_shared.accounts.factories import GroupFactory, UserFactory
from adit_radis_shared.common.utils.testing_helpers import add_user_to_group
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from radis.reports.factories import LanguageFactory, ReportFactory
from radis.reports.models import Report

pytestmark = pytest.mark.django_db


def login_with_active_group(client: Client):
    user = UserFactory.create(is_active=True)
    group = GroupFactory.create()
    add_user_to_group(user, group)
    client.force_login(user)
    return user, group


def create_report_for(group) -> Report:
    report = ReportFactory.create(language=LanguageFactory.create(code="en"))
    report.groups.add(group)
    return report


def test_report_detail_view_shows_live_report(client: Client):
    _, group = login_with_active_group(client)
    report = create_report_for(group)

    response = client.get(reverse("report_detail", args=[report.pk]))

    assert response.status_code == 200


def test_report_detail_view_404s_for_withdrawn_report(client: Client):
    _, group = login_with_active_group(client)
    report = create_report_for(group)
    Report.objects.filter(pk=report.pk).update(withdrawn_at=timezone.now())

    response = client.get(reverse("report_detail", args=[report.pk]))

    assert response.status_code == 404


def test_report_body_view_404s_for_withdrawn_report(client: Client):
    _, group = login_with_active_group(client)
    report = create_report_for(group)
    Report.objects.filter(pk=report.pk).update(withdrawn_at=timezone.now())

    response = client.get(reverse("report_body", args=[report.pk]))

    assert response.status_code == 404


def test_report_list_view_hides_withdrawn_reports(client: Client):
    _, group = login_with_active_group(client)
    live = create_report_for(group)
    withdrawn = create_report_for(group)
    Report.objects.filter(pk=withdrawn.pk).update(withdrawn_at=timezone.now())

    response = client.get(reverse("report_list"))

    assert response.status_code == 200
    reports = list(response.context["reports"])
    assert live in reports
    assert withdrawn not in reports
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest radis/reports/tests/test_views.py -v`
Expected: the two 404 tests and the list test FAIL (status 200 / withdrawn report present); the live-report test passes already.

- [ ] **Step 3: Apply `.live()` in the views**

In `radis/reports/views.py`:

`ReportListView.get_queryset` — replace the body with:

```python
    def get_queryset(self) -> QuerySet[Report]:
        return Report.objects.live().filter(groups=self.request.user.active_group).order_by(
            "-study_datetime"
        )
```

`ReportDetailView.get_queryset` — replace the whole method with:

```python
    def get_queryset(self) -> QuerySet[Report]:
        active_group = self.request.user.active_group
        assert active_group
        return Report.objects.live().filter(groups=active_group)
```

(Equivalent to the previous `super().get_queryset()` chain — the default manager's `.all()` — but typed as `ReportQuerySet` so `.live()` resolves cleanly. `ReportBodyView` inherits this method.)

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest radis/reports/tests/test_views.py -v`
Expected: 4 passed.

- [ ] **Step 5: Lint and commit**

```bash
uv run cli lint
git add radis/reports/views.py radis/reports/tests/test_views.py
git commit -m "Hide withdrawn reports from the report list and detail views"
```

---

### Task 8: Collections hide withdrawn reports

**Files:**
- Modify: `radis/collections/views.py` (`CollectionDetailView.get_queryset`)
- Modify: `radis/collections/utils/exporters.py` (`export_collection`)
- Test: `radis/collections/tests/test_views.py`, `radis/collections/tests/test_exporters.py` (extend)

**Interfaces:**
- Consumes: `.live()` on related managers (Task 1: `collection.reports.live()`).
- Produces: collection page and Excel export skip withdrawn reports; the collection membership row survives untouched (spec decision 5).

- [ ] **Step 1: Write the failing tests**

Add to `radis/collections/tests/test_views.py` (the module already imports `pytest`, `UserFactory`, `Client`, `CollectionFactory`, `LanguageFactory`, `ReportFactory` and defines `create_test_report()`; extend the imports with):

```python
from django.utils import timezone

from radis.reports.models import Report
```

Then add:

```python
@pytest.mark.django_db
def test_collection_detail_view_hides_withdrawn_reports(client: Client):
    user = UserFactory.create(is_active=True)
    collection = CollectionFactory.create(owner=user)
    live = create_test_report()
    withdrawn = create_test_report()
    collection.reports.add(live, withdrawn)
    Report.objects.filter(pk=withdrawn.pk).update(withdrawn_at=timezone.now())
    client.force_login(user)

    response = client.get(f"/collections/{collection.pk}/")

    assert response.status_code == 200
    reports = list(response.context["reports"])
    assert live in reports
    assert withdrawn not in reports
    # The membership row survives; only the listing hides it.
    assert collection.reports.count() == 2
```

Add to `radis/collections/tests/test_exporters.py` (extend its imports with):

```python
from django.utils import timezone

from radis.reports.models import Report
```

Then add:

```python
@pytest.mark.django_db
def test_export_collection_skips_withdrawn_reports():
    user = UserFactory.create(is_active=True)
    collection = CollectionFactory.create(owner=user)
    language = LanguageFactory.create(code="en")
    live = ReportFactory.create(language=language, patient_id="live-patient")
    withdrawn = ReportFactory.create(language=language, patient_id="withdrawn-patient")
    collection.reports.add(live, withdrawn)
    Report.objects.filter(pk=withdrawn.pk).update(withdrawn_at=timezone.now())

    ws = _load_sheet(collection)

    patient_ids = [row[1] for row in ws.iter_rows(min_row=2, values_only=True)]
    assert patient_ids == ["live-patient"]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest radis/collections/tests/ -k withdrawn -v`
Expected: 2 FAILED — the withdrawn report appears in the context list and in the exported sheet.

- [ ] **Step 3: Apply `.live()`**

In `radis/collections/views.py`, `CollectionDetailView.get_queryset` — replace

```python
    def get_queryset(self) -> QuerySet[Report]:
        return cast(Collection, self.object).reports.all()
```

with

```python
    def get_queryset(self) -> QuerySet[Report]:
        return cast(Collection, self.object).reports.live()
```

In `radis/collections/utils/exporters.py`, in `export_collection`, replace

```python
    for report in collection.reports.all():
```

with

```python
    for report in collection.reports.live():
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest radis/collections/tests/ -v`
Expected: all pass (pre-existing collection tests included).

- [ ] **Step 5: Lint and commit**

```bash
uv run cli lint
git add radis/collections/views.py radis/collections/utils/exporters.py radis/collections/tests/
git commit -m "Hide withdrawn reports from collection listings and exports"
```

---

### Task 9: Notes hide withdrawn reports

**Files:**
- Modify: `radis/notes/views.py`
- Test: `radis/notes/tests/test_views.py` (extend)

**Interfaces:**
- Consumes: `Report.objects.live()` (Task 1).
- Produces: the notes list and detail skip notes on withdrawn reports; `note_edit` and `note_available_badge` 404 for a withdrawn report. Note rows survive and reappear on restore (spec decision 5).

- [ ] **Step 1: Write the failing tests**

Add to `radis/notes/tests/test_views.py` (extend its imports with):

```python
from django.utils import timezone

from radis.reports.models import Report
```

Then add:

```python
@pytest.mark.django_db
def test_note_list_view_hides_notes_on_withdrawn_reports(client: Client):
    user = UserFactory.create(is_active=True)
    live_report = create_test_report()
    withdrawn_report = create_test_report()
    live_note = NoteFactory.create(owner=user, report=live_report)
    withdrawn_note = NoteFactory.create(owner=user, report=withdrawn_report)
    Report.objects.filter(pk=withdrawn_report.pk).update(withdrawn_at=timezone.now())
    client.force_login(user)

    response = client.get(reverse("note_list"))

    assert response.status_code == 200
    assert live_note in response.context["notes"]
    assert withdrawn_note not in response.context["notes"]
    # The note itself survives for when the report is restored.
    assert Note.objects.filter(pk=withdrawn_note.pk).exists()


@pytest.mark.django_db
def test_note_edit_view_404s_for_withdrawn_report(client: Client):
    user = UserFactory.create(is_active=True)
    report = create_test_report()
    Report.objects.filter(pk=report.pk).update(withdrawn_at=timezone.now())
    client.force_login(user)

    response = client.post(
        reverse("note_edit", args=[report.pk]),
        {"text": "should never be saved"},
        headers={"HX-Request": "true"},
    )

    assert response.status_code == 404
    assert not Note.objects.filter(report=report).exists()


@pytest.mark.django_db
def test_note_available_badge_404s_for_withdrawn_report(client: Client):
    user = UserFactory.create(is_active=True)
    report = create_test_report()
    Report.objects.filter(pk=report.pk).update(withdrawn_at=timezone.now())
    client.force_login(user)

    response = client.get(
        reverse("note_available_badge", args=[report.pk]),
        headers={"HX-Request": "true"},
    )

    assert response.status_code == 404
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest radis/notes/tests/test_views.py -k withdrawn -v`
Expected: 3 FAILED (note listed, note saved / 204, badge 200).

- [ ] **Step 3: Apply the filters**

In `radis/notes/views.py`:

Extend the imports with:

```python
from django.shortcuts import get_object_or_404
```

(`render` is already imported from `django.shortcuts`; merge into one import line.)

`NoteListView.get_queryset` — replace the body with:

```python
    def get_queryset(self) -> QuerySet[Note]:
        return Note.objects.filter(
            owner=self.request.user, report__withdrawn_at__isnull=True
        ).order_by("-pk")
```

`NoteDetailView.get_queryset` — replace the body with:

```python
    def get_queryset(self) -> QuerySet[Note]:
        return Note.objects.filter(owner=self.request.user, report__withdrawn_at__isnull=True)
```

`NoteEditView` — add as its first method (before `get_object`):

```python
    def dispatch(self, request, *args, **kwargs):
        # Creating or editing a note must not resurrect access to a withdrawn
        # report; the guard covers GET (dialog) and POST (save) alike.
        get_object_or_404(Report.objects.live(), pk=self.kwargs["report_id"])
        return super().dispatch(request, *args, **kwargs)
```

`NoteAvailableBadgeView.get` — replace

```python
        report = Report.objects.get(id=report_id)
```

with

```python
        report = get_object_or_404(Report.objects.live(), id=report_id)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest radis/notes/tests/test_views.py -v`
Expected: all pass (pre-existing notes tests included).

- [ ] **Step 5: Lint and commit**

```bash
uv run cli lint
git add radis/notes/views.py radis/notes/tests/test_views.py
git commit -m "Hide withdrawn reports from notes views"
```

---

### Task 10: Chats hide withdrawn reports (report-less chats stay)

**Files:**
- Modify: `radis/chats/views.py`
- Test: `radis/chats/tests/test_views.py` (extend)

**Interfaces:**
- Consumes: `Report.objects.live()` (Task 1).
- Produces: chats bound to a withdrawn report disappear from the chat list and 404 on open/update; chats with `report=None` (general chats) are untouched — `Chat.report` is nullable and the filter must preserve that.

- [ ] **Step 1: Write the failing tests**

Add to `radis/chats/tests/test_views.py` (extend the imports with what is missing):

```python
from django.test import Client
from django.utils import timezone

from radis.reports.models import Report
```

Then add:

```python
@pytest.mark.django_db
def test_chat_list_hides_chats_on_withdrawn_reports(client: Client):
    user = UserFactory.create(is_active=True)
    Chat.objects.create(owner=user, title="general chat")
    report = ReportFactory.create()
    Chat.objects.create(owner=user, title="report chat", report=report)
    Report.objects.filter(pk=report.pk).update(withdrawn_at=timezone.now())
    client.force_login(user)

    response = client.get(reverse("chat_list"))

    content = response.content.decode()
    assert "general chat" in content
    assert "report chat" not in content


@pytest.mark.django_db
def test_chat_detail_404s_when_report_withdrawn(client: Client):
    user = UserFactory.create(is_active=True)
    report = ReportFactory.create()
    chat = Chat.objects.create(owner=user, title="report chat", report=report)
    Report.objects.filter(pk=report.pk).update(withdrawn_at=timezone.now())
    client.force_login(user)

    response = client.get(reverse("chat_detail", args=[chat.pk]))

    assert response.status_code == 404


@pytest.mark.django_db
def test_chat_detail_still_serves_reportless_chats(client: Client):
    user = UserFactory.create(is_active=True)
    chat = Chat.objects.create(owner=user, title="general chat")
    client.force_login(user)

    with _stub_render():
        response = client.get(reverse("chat_detail", args=[chat.pk]))

    assert response.status_code == 200
```

(`_stub_render` is the module's existing helper that replaces `views.render`; the 404 paths never render, so only the 200 case needs it.)

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest radis/chats/tests/test_views.py -k "withdrawn or reportless" -v`
Expected: the first two FAIL (chat listed / 200); the report-less test passes already — keep it anyway, it pins the regression this task could introduce.

- [ ] **Step 3: Apply the visibility filter**

In `radis/chats/views.py`:

Extend the imports with:

```python
from django.db.models import Q
```

Add a module-level helper above `chat_list_view`:

```python
def _visible_chats():
    """Chats whose report is still in circulation. report is nullable --
    general chats have none and must always stay visible."""
    return Chat.objects.filter(Q(report__isnull=True) | Q(report__withdrawn_at__isnull=True))
```

Then apply it:

- `chat_list_view`: replace `chats = Chat.objects.filter(owner=request.user)` with `chats = _visible_chats().filter(owner=request.user)`.
- `chat_detail_view`: replace `chat = get_object_or_404(Chat, pk=pk, owner=request.user)` with `chat = get_object_or_404(_visible_chats(), pk=pk, owner=request.user)`.
- `chat_update_view`: replace `chat = await aget_object_or_404(Chat.objects.prefetch_related("report"), pk=pk, owner=request.user)` with `chat = await aget_object_or_404(_visible_chats().prefetch_related("report"), pk=pk, owner=request.user)`.
- `chat_create_view`, POST branch: replace `report = await aget_object_or_404(Report, pk=report_id)` with `report = await aget_object_or_404(Report.objects.live(), pk=report_id)`.
- `chat_create_view`, GET branch: replace `report = await aget_object_or_404(Report, id=report_id, groups=active_group)` with `report = await aget_object_or_404(Report.objects.live(), id=report_id, groups=active_group)`.

(`chat_delete_view` and `chat_clear_all` stay unfiltered on purpose: deleting one's own chat about a withdrawn report is cleanup, not access.)

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest radis/chats/tests/test_views.py -v`
Expected: all pass (the pre-existing async LLM tests included).

- [ ] **Step 5: Lint and commit**

```bash
uv run cli lint
git add radis/chats/views.py radis/chats/tests/test_views.py
git commit -m "Hide chats on withdrawn reports while keeping general chats"
```

---

### Task 11: Subscriptions — inbox filter and worker re-check

**Files:**
- Modify: `radis/subscriptions/views.py` (`SubscriptionInboxView.get_related_queryset`, `SubscriptionInboxDownloadView.get_related_queryset`)
- Modify: `radis/subscriptions/processors.py` (`SubscriptionTaskProcessor.process_task`)
- Test: `radis/subscriptions/tests/test_views.py`, `radis/subscriptions/tests/test_processors.py` (extend)

**Interfaces:**
- Consumes: `.live()` on related managers (Task 1). The refresh path (`filter_provider.filter`) is already covered by Task 5 — do not re-filter it.
- Produces: inbox and its CSV download hide items of withdrawn reports (rows survive, spec decision 5); a report withdrawn between refresh and task execution is never sent to the LLM or emailed.

- [ ] **Step 1: Write the failing tests**

Add to `radis/subscriptions/tests/test_views.py` (extend the imports with what is missing):

```python
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from radis.reports.factories import LanguageFactory, ReportFactory
from radis.reports.models import Report
from radis.subscriptions.factories import SubscriptionFactory
from radis.subscriptions.models import SubscribedItem
```

Then add:

```python
@pytest.mark.django_db
def test_subscription_inbox_hides_withdrawn_reports(client: Client):
    user = UserFactory.create(is_active=True)
    subscription = SubscriptionFactory.create(owner=user)
    live = ReportFactory.create(language=LanguageFactory.create(code="en"))
    withdrawn = ReportFactory.create(language=LanguageFactory.create(code="en"))
    live_item = SubscribedItem.objects.create(subscription=subscription, report=live)
    SubscribedItem.objects.create(subscription=subscription, report=withdrawn)
    Report.objects.filter(pk=withdrawn.pk).update(withdrawn_at=timezone.now())
    client.force_login(user)

    response = client.get(reverse("subscription_inbox", args=[subscription.pk]))

    assert response.status_code == 200
    assert list(response.context["object_list"]) == [live_item]
    # The inbox row survives for when the report is restored.
    assert SubscribedItem.objects.filter(subscription=subscription).count() == 2
```

Add to `radis/subscriptions/tests/test_processors.py` (extend the imports with what is missing):

```python
from django.utils import timezone

from radis.reports.models import Report
```

Then add:

```python
@pytest.mark.django_db
def test_process_task_skips_withdrawn_reports(monkeypatch):
    task = _make_task_with_reports(["Is this relevant?"], num_reports=2)
    withdrawn = task.reports.order_by("pk").first()
    assert withdrawn is not None
    Report.objects.filter(pk=withdrawn.pk).update(withdrawn_at=timezone.now())

    processed: list[int] = []
    monkeypatch.setattr(
        SubscriptionTaskProcessor,
        "process_report",
        lambda self, report, task: processed.append(report.pk),
    )
    SubscriptionTaskProcessor(task).process_task(task)

    assert withdrawn.pk not in processed
    assert set(processed) == set(task.reports.live().values_list("pk", flat=True))
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest radis/subscriptions/tests/test_views.py radis/subscriptions/tests/test_processors.py -k withdrawn -v`
Expected: 2 FAILED — the withdrawn item is in `object_list`; the withdrawn report's pk is in `processed`.

- [ ] **Step 3: Apply the filters**

In `radis/subscriptions/views.py`, in **both** `SubscriptionInboxView.get_related_queryset` and `SubscriptionInboxDownloadView.get_related_queryset`, insert `.filter(report__withdrawn_at__isnull=True)` directly after `SubscribedItem.objects.filter(subscription_id=subscription.pk)` (keep every other chained call unchanged). For the inbox view that means:

```python
        return (
            SubscribedItem.objects.filter(subscription_id=subscription.pk)
            .filter(report__withdrawn_at__isnull=True)
            .select_related("subscription")
            .prefetch_related(
                "report",
                "subscription__output_fields",
            )
            .order_by(ordering)
        )
```

and for the download view:

```python
        return (
            SubscribedItem.objects.filter(subscription_id=subscription.pk)
            .filter(report__withdrawn_at__isnull=True)
            .exclude(extraction_results__isnull=True)  # Only items with results
            .exclude(extraction_results={})  # Only items with non-empty results
            .select_related("subscription")
            .prefetch_related(
                "report",
                "report__modalities",
                "subscription__output_fields",
            )
            .order_by(ordering)
        )
```

In `radis/subscriptions/processors.py`, in `process_task`, replace

```python
                for report in task.reports.filter(groups=active_group):
```

with

```python
                # live(): a report can be withdrawn between the refresh that
                # selected it and this task running -- never gate or email it.
                for report in task.reports.live().filter(groups=active_group):
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest radis/subscriptions/tests/test_views.py radis/subscriptions/tests/test_processors.py -v`
Expected: all pass (pre-existing subscription tests included).

- [ ] **Step 5: Lint and commit**

```bash
uv run cli lint
git add radis/subscriptions/views.py radis/subscriptions/processors.py radis/subscriptions/tests/
git commit -m "Keep withdrawn reports out of subscription inboxes and tasks"
```

---

### Task 12: Extractions worker re-check

**Files:**
- Modify: `radis/extractions/processors.py` (`ExtractionTaskProcessor.process_task`)
- Test: `radis/extractions/tests/test_processors.py` (extend)

**Interfaces:**
- Consumes: `Report.withdrawn_at` (Task 1). Job creation already excludes withdrawn reports via Task 5 (`count()`/`retrieve()`); this closes only the race between job creation and task execution.
- Produces: instances whose report was withdrawn in the meantime are skipped (left `is_processed=False`; they simply never process while the report stays withdrawn).

- [ ] **Step 1: Write the failing test**

Add to `radis/extractions/tests/test_processors.py` (extend the imports with what is missing):

```python
from django.utils import timezone

from radis.extractions.factories import ExtractionInstanceFactory, ExtractionTaskFactory
from radis.extractions.processors import ExtractionTaskProcessor
from radis.reports.models import Report
```

Then add:

```python
@pytest.mark.django_db
def test_process_task_skips_instances_of_withdrawn_reports(monkeypatch):
    task = ExtractionTaskFactory.create()
    live = ExtractionInstanceFactory.create(task=task, is_processed=False)
    withdrawn = ExtractionInstanceFactory.create(task=task, is_processed=False)
    Report.objects.filter(pk=withdrawn.report.pk).update(withdrawn_at=timezone.now())

    processed: list[int] = []
    monkeypatch.setattr(
        ExtractionTaskProcessor,
        "process_instance",
        lambda self, instance: processed.append(instance.pk),
    )
    ExtractionTaskProcessor(task).process_task(task)

    assert processed == [live.pk]
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest radis/extractions/tests/test_processors.py -k withdrawn -v`
Expected: FAIL — both instance pks are in `processed`.

- [ ] **Step 3: Apply the filter**

In `radis/extractions/processors.py`, in `process_task`, replace

```python
                # Skip instances an earlier run of this task (killed mid-way) already processed.
                for instance in task.instances.filter(is_processed=False):
```

with

```python
                # Skip instances an earlier run of this task (killed mid-way)
                # already processed, and reports withdrawn since job creation.
                for instance in task.instances.filter(
                    is_processed=False, report__withdrawn_at__isnull=True
                ):
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest radis/extractions/tests/test_processors.py -v`
Expected: all pass.

- [ ] **Step 5: Lint and commit**

```bash
uv run cli lint
git add radis/extractions/processors.py radis/extractions/tests/test_processors.py
git commit -m "Skip withdrawn reports in extraction task processing"
```

---

### Task 13: Admin withdraw action

**Files:**
- Modify: `radis/reports/admin.py`
- Create: `radis/reports/templates/admin/reports/report/withdraw_selected_confirmation.html`
- Test: `radis/reports/tests/test_admin.py` (new file)

**Interfaces:**
- Consumes: the Task 1 fields; the Task 4 trigger (a `queryset.update()` of `withdrawn_at` is all that's needed to update search).
- Produces: `ReportAdmin.withdraw_selected` action (used verbatim by Task 14's inherited admin), `WithdrawReportsForm`, the confirmation template. `readonly_fields` makes the three withdrawal fields un-editable on the change form.

- [ ] **Step 1: Write the failing tests**

Create `radis/reports/tests/test_admin.py`:

```python
from datetime import timedelta

import pytest
from django.contrib.admin import helpers
from django.contrib.admin.models import CHANGE, LogEntry
from django.urls import reverse
from django.utils import timezone

from radis.pgsearch.models import ReportSearchIndex
from radis.reports.factories import LanguageFactory, ReportFactory
from radis.reports.models import Report

pytestmark = pytest.mark.django_db

CHANGELIST_URL = reverse("admin:reports_report_changelist")


def create_report() -> Report:
    return ReportFactory.create(language=LanguageFactory.create(code="en"))


def test_withdraw_action_asks_for_a_reason(admin_client):
    report = create_report()

    response = admin_client.post(
        CHANGELIST_URL,
        {"action": "withdraw_selected", helpers.ACTION_CHECKBOX_NAME: [str(report.pk)]},
    )

    assert response.status_code == 200
    assert b"Reason for withdrawal" in response.content
    report.refresh_from_db()
    assert not report.is_withdrawn


def test_withdraw_action_sets_state_logs_and_syncs_projection(admin_client):
    report = create_report()

    response = admin_client.post(
        CHANGELIST_URL,
        {
            "action": "withdraw_selected",
            helpers.ACTION_CHECKBOX_NAME: [str(report.pk)],
            "apply": "1",
            "reason": "wrong patient",
        },
        follow=True,
    )

    assert response.status_code == 200
    report.refresh_from_db()
    assert report.is_withdrawn
    assert report.withdrawn_by is not None
    assert report.withdrawal_reason == "wrong patient"
    assert ReportSearchIndex.objects.get(report=report).withdrawn is True
    log = LogEntry.objects.get(object_id=str(report.pk), action_flag=CHANGE)
    assert "wrong patient" in log.change_message


def test_withdraw_action_requires_a_reason(admin_client):
    report = create_report()

    response = admin_client.post(
        CHANGELIST_URL,
        {
            "action": "withdraw_selected",
            helpers.ACTION_CHECKBOX_NAME: [str(report.pk)],
            "apply": "1",
            "reason": "",
        },
    )

    assert response.status_code == 200
    report.refresh_from_db()
    assert not report.is_withdrawn


def test_withdraw_action_skips_already_withdrawn_reports(admin_client):
    already = create_report()
    original_time = timezone.now() - timedelta(days=1)
    Report.objects.filter(pk=already.pk).update(
        withdrawn_at=original_time, withdrawal_reason="original reason"
    )
    fresh = create_report()

    admin_client.post(
        CHANGELIST_URL,
        {
            "action": "withdraw_selected",
            helpers.ACTION_CHECKBOX_NAME: [str(already.pk), str(fresh.pk)],
            "apply": "1",
            "reason": "new reason",
        },
    )

    already.refresh_from_db()
    fresh.refresh_from_db()
    assert already.withdrawal_reason == "original reason"
    assert already.withdrawn_at == original_time
    assert fresh.withdrawal_reason == "new reason"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest radis/reports/tests/test_admin.py -v`
Expected: FAIL — the action doesn't exist yet, so Django falls through with "No action selected" (status 302/200 without effect).

- [ ] **Step 3: Implement the action**

In `radis/reports/admin.py`:

Extend the imports with:

```python
from django import forms
from django.contrib.admin import helpers
from django.template.response import TemplateResponse
from django.utils import timezone
```

Add above `class ReportAdmin`:

```python
class WithdrawReportsForm(forms.Form):
    reason = forms.CharField(
        label="Reason for withdrawal",
        widget=forms.Textarea(attrs={"rows": 3}),
    )
```

Add inside `ReportAdmin` (attributes near the top of the class, methods after the existing ones):

```python
    list_display = ("__str__", "is_withdrawn_display")
    list_filter = (("withdrawn_at", admin.EmptyFieldListFilter),)
    readonly_fields = ("withdrawn_at", "withdrawn_by", "withdrawal_reason")
    actions = ("withdraw_selected",)

    @admin.display(boolean=True, description="Withdrawn")
    def is_withdrawn_display(self, obj: Report) -> bool:
        return obj.is_withdrawn

    @admin.action(description="Withdraw selected reports", permissions=["change"])
    def withdraw_selected(
        self, request: HttpRequest, queryset: QuerySet[Report]
    ) -> HttpResponse | None:
        queryset = queryset.filter(withdrawn_at__isnull=True)
        form = WithdrawReportsForm(request.POST if "apply" in request.POST else None)
        if "apply" in request.POST and form.is_valid():
            reason = form.cleaned_data["reason"]
            reports = list(queryset)
            # update() on purpose: the projection trigger is the only sync a
            # state flip needs; skipping post_save keeps re-embedding and
            # label staleness out of it.
            queryset.update(
                withdrawn_at=timezone.now(),
                withdrawn_by=request.user,
                withdrawal_reason=reason,
            )
            for report in reports:
                self.log_change(request, report, f"Withdrawn: {reason}")
            self.message_user(request, f"Withdrew {len(reports)} report(s).", messages.SUCCESS)
            return None
        return TemplateResponse(
            request,
            "admin/reports/report/withdraw_selected_confirmation.html",
            {
                **self.admin_site.each_context(request),
                "title": "Withdraw reports",
                "queryset": queryset,
                "form": form,
                "opts": self.model._meta,
                "action_checkbox_name": helpers.ACTION_CHECKBOX_NAME,
            },
        )
```

- [ ] **Step 4: Create the confirmation template**

Create `radis/reports/templates/admin/reports/report/withdraw_selected_confirmation.html`:

```html
{% extends "admin/base_site.html" %}
{% load admin_urls %}
{% block content %}
    <p>
        You are about to withdraw the following {{ queryset.count }} report(s).
        They disappear from search and every user-facing page until an admin
        restores them from the "Withdrawn reports" listing.
    </p>
    <ul>
        {% for report in queryset %}<li>{{ report }}</li>{% endfor %}
    </ul>
    <form method="post">
        {% csrf_token %}
        {{ form.as_p }}
        {% for report in queryset %}
            <input type="hidden" name="{{ action_checkbox_name }}" value="{{ report.pk }}">
        {% endfor %}
        <input type="hidden" name="action" value="withdraw_selected">
        <input type="hidden" name="apply" value="1">
        <button type="submit" class="button">Withdraw</button>
        <a href="{% url opts|admin_urlname:'changelist' %}" class="button cancel-link">Cancel</a>
    </form>
{% endblock %}
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest radis/reports/tests/test_admin.py -v`
Expected: 4 passed.

- [ ] **Step 6: Lint and commit**

```bash
uv run cli lint
git add radis/reports/admin.py radis/reports/templates/ radis/reports/tests/test_admin.py
git commit -m "Add the admin withdraw action with a mandatory reason"
```

---

### Task 14: WithdrawnReport proxy page with restore

**Files:**
- Modify: `radis/reports/models.py` (proxy model), `radis/reports/admin.py`
- Create: `radis/reports/migrations/0016_withdrawnreport_proxy.py` (via makemigrations)
- Test: `radis/reports/tests/test_admin.py` (extend)

**Interfaces:**
- Consumes: `ReportAdmin` incl. `withdraw_selected` infrastructure (Task 13), the 0015 partial index (Task 2) which serves this listing's `withdrawn_at IS NOT NULL` filter.
- Produces: the dedicated "Withdrawn reports" admin entry with `restore_selected`. Nothing downstream consumes it.

- [ ] **Step 1: Write the failing tests**

Add to `radis/reports/tests/test_admin.py`:

```python
WITHDRAWN_CHANGELIST_URL = reverse("admin:reports_withdrawnreport_changelist")


def withdraw(report: Report) -> None:
    Report.objects.filter(pk=report.pk).update(
        withdrawn_at=timezone.now(), withdrawal_reason="test reason"
    )


def test_withdrawn_listing_shows_only_withdrawn_reports(admin_client):
    live = create_report()
    gone = create_report()
    withdraw(gone)

    response = admin_client.get(WITHDRAWN_CHANGELIST_URL)

    content = response.content.decode()
    assert gone.document_id in content
    assert live.document_id not in content


def test_withdrawn_listing_has_no_add_button(admin_client):
    response = admin_client.get(reverse("admin:reports_withdrawnreport_add"))

    assert response.status_code == 403


def test_restore_action_clears_state_logs_and_resyncs_projection(admin_client):
    report = create_report()
    withdraw(report)

    response = admin_client.post(
        WITHDRAWN_CHANGELIST_URL,
        {"action": "restore_selected", helpers.ACTION_CHECKBOX_NAME: [str(report.pk)]},
        follow=True,
    )

    assert response.status_code == 200
    report.refresh_from_db()
    assert not report.is_withdrawn
    assert report.withdrawn_by is None
    assert report.withdrawal_reason == ""
    assert ReportSearchIndex.objects.get(report=report).withdrawn is False
    logs = LogEntry.objects.filter(object_id=str(report.pk), action_flag=CHANGE)
    assert any("Restored" in entry.change_message for entry in logs)
```

NOTE (reverse() at import time): `WITHDRAWN_CHANGELIST_URL` resolves only once the proxy admin exists, so the whole test module fails on import until Step 3 — that IS the failing state for this task.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest radis/reports/tests/test_admin.py -v`
Expected: ERROR at collection — `NoReverseMatch` for `reports_withdrawnreport_changelist`.

- [ ] **Step 3: Add the proxy model and admin**

In `radis/reports/models.py`, after the `Report` class (before `Metadata`), add:

```python
class WithdrawnReport(Report):
    """Withdrawn reports as their own admin entry -- the dedicated listing
    the restore action lives on."""

    class Meta:
        proxy = True
        verbose_name = "Withdrawn report"
```

In `radis/reports/admin.py`: extend the models import to include `WithdrawnReport`, then add after `admin.site.register(Report, ReportAdmin)`:

```python
class WithdrawnReportAdmin(ReportAdmin):
    """Inherits ReportAdmin so hard delete keeps firing the index-cleanup
    handlers and the change form keeps its read-only withdrawal fields."""

    list_display = (
        "document_id",
        "patient_id",
        "study_description",
        "withdrawn_at",
        "withdrawn_by",
        "withdrawal_reason",
    )
    list_filter = ()
    actions = ("restore_selected",)

    def get_queryset(self, request: HttpRequest) -> QuerySet[Report]:
        return super().get_queryset(request).filter(withdrawn_at__isnull=False)

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    @admin.action(description="Restore selected reports", permissions=["change"])
    def restore_selected(self, request: HttpRequest, queryset: QuerySet[Report]) -> None:
        reports = list(queryset)
        # update() for the same reason as withdraw_selected: the projection
        # trigger is the only sync a state flip needs.
        queryset.update(withdrawn_at=None, withdrawn_by=None, withdrawal_reason="")
        for report in reports:
            self.log_change(request, report, "Restored from withdrawal")
        self.message_user(request, f"Restored {len(reports)} report(s).", messages.SUCCESS)


admin.site.register(WithdrawnReport, WithdrawnReportAdmin)
```

- [ ] **Step 4: Generate the proxy migration**

Run: `uv run ./manage.py makemigrations reports -n withdrawnreport_proxy`
Expected: `0016_withdrawnreport_proxy.py` with one `CreateModel` whose options contain `"proxy": True` and whose bases are `("reports.report",)` — state-only, no SQL (verify with `uv run ./manage.py sqlmigrate reports 0016`: no CREATE TABLE).

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest radis/reports/tests/test_admin.py -v`
Expected: 7 passed (Task 13's four included).

- [ ] **Step 6: Lint and commit**

```bash
uv run cli lint
git add radis/reports/models.py radis/reports/admin.py radis/reports/migrations/0016_withdrawnreport_proxy.py radis/reports/tests/test_admin.py
git commit -m "Add the withdrawn reports admin page with a restore action"
```

---

### Task 15: Documentation and full-suite verification

**Files:**
- Modify: `docs/user-docs/admin-guide.md` (append a section)

**Interfaces:**
- Consumes: the finished feature (Tasks 1–14).
- Produces: operator documentation; a verified green branch.

- [ ] **Step 1: Append the admin-guide section**

Append to `docs/user-docs/admin-guide.md`:

```markdown
## Withdrawing Reports

Withdrawing takes a report out of circulation without deleting it: it
disappears from search, report lists, collections, notes, chats and
subscription inboxes, and running extraction or subscription jobs skip it. The
report row itself is kept and can be restored at any time.

### Withdraw

1. **Navigate** to **Django Admin** --> **Reports**.
2. **Select** the reports to withdraw using the checkboxes.
3. **Choose** the action **"Withdraw selected reports"** and click **"Go"**.
4. **Enter a reason** on the confirmation page and confirm. The reason, the
   acting user and the timestamp are stored on the report.

### Restore

1. **Navigate** to **Django Admin** --> **Withdrawn reports** (the dedicated
   listing; it shows who withdrew each report, when and why).
2. **Select** the reports and run **"Restore selected reports"**.
3. Restored reports reappear everywhere immediately. Restoring clears the
   stored reason/user/timestamp; the admin log keeps a record of both actions.

### Withdrawn Reports and the API

The REST API (admin-only) still returns withdrawn reports and marks them with
the read-only fields `withdrawn` and `withdrawn_at`. An upsert
(`PUT ...?upsert=true` or bulk upsert) that targets a withdrawn report updates
its content but does **not** restore it -- re-imports never undo a withdrawal.
The bulk upsert response lists the affected ids under `"withdrawn"`. Hard
deletion via the API or admin remains available and is unaffected.
```

- [ ] **Step 2: Verify no migration is missing**

Run: `uv run ./manage.py makemigrations --check --dry-run`
Expected: "No changes detected".

- [ ] **Step 3: Run the full test suite**

Run: `uv run pytest radis/ -m "not acceptance" -q`
Expected: all pass, no new failures anywhere (acceptance tests need the dev containers and are out of scope here).

- [ ] **Step 4: Lint everything**

Run: `uv run cli lint`
Expected: clean.

- [ ] **Step 5: Commit**

```bash
git add docs/user-docs/admin-guide.md
git commit -m "Document report withdrawal in the admin guide"
```

---

## Out of Scope (do not build)

Per the spec: no retention/auto-expiry, no REST endpoint for withdraw/restore, no withdrawal-event history table, and no exclusion of withdrawn reports from the labels or embeddings machinery — those deliberately keep seeing the full corpus.
