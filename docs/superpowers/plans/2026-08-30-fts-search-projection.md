# FTS Search Projection Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the full-text search candidate query single-table by completing `ReportSearchIndex` as a search projection, so it stops joining three tables and applying `SELECT DISTINCT`.

**Architecture:** `ReportSearchIndex` already mirrors derived data (`search_vector`, `embedding`). This adds ten more mirrored columns — the fields search *filters* on — maintained by statement-level PostgreSQL triggers rather than application signals, because `group_ids` is access-control data and must stay correct for every writer including raw SQL. `_build_filter_query` then becomes a single-table predicate builder and `.distinct()` disappears.

**Tech Stack:** Django 6.0, PostgreSQL 17, pytest + pytest-django, factory-boy.

**Spec:** `docs/superpowers/specs/2026-08-29-fts-query-shape-performance-design.md`

## Global Constraints

- **Design target: 8 million reports.** Measured there: `8,388 ms → 882 ms`.
- **Access control is the risk surface.** `group_ids` decides who may read a patient's report. Any change touching it needs a test.
- **`filters.group=None` is fail-closed** and must match only reports in no group — `Q(group_ids=[])`, never a `contains` variant.
- **Triggers must be `AFTER`, never `BEFORE`.** PostgreSQL does not expose stored generated column values to BEFORE triggers, so `patient_age` would mirror as NULL. Verified.
- **Migration order is load-bearing:** columns → triggers → backfill → indexes. Triggers before backfill so concurrent writes during a ~9-minute backfill still land.
- Line length 100 (Ruff), Google Python style, type checking pyright basic.
- Run tests with `uv run cli test -- <args>`.

**Deviation from the spec, deliberate:** §4.4 describes one migration. This plan splits it into four (`0003`–`0006`), one per ordered step. Same operations, same order — but each is independently applicable and testable, and no task has to edit a migration a previous task already applied.

## File Structure

| File | Responsibility |
| --- | --- |
| `radis/pgsearch/models.py` | Add the ten projection fields + two indexes to `ReportSearchIndex` |
| `radis/pgsearch/utils/projection.py` | **New.** The canonical projection SQL and `sync_projection(report_ids)`; single source of truth for the write path |
| `radis/pgsearch/migrations/0003_search_projection_columns.py` | **New.** `AddField` ×10 |
| `radis/pgsearch/migrations/0004_search_projection_triggers.py` | **New.** Three trigger functions + five triggers |
| `radis/pgsearch/migrations/0005_search_projection_backfill.py` | **New.** Chunked backfill, `atomic = False` |
| `radis/pgsearch/migrations/0006_search_projection_indexes.py` | **New.** Two indexes + GIN `fastupdate=off` |
| `radis/pgsearch/management/commands/check_search_projection.py` | **New.** Drift detection |
| `radis/pgsearch/providers.py` | `_build_filter_query`, `match_q`, `rank_expr`, `summary_expr`, `_exclude_negations` go single-table |
| `radis/pgsearch/signals.py` | Populate the projection when a row is created |
| `radis/pgsearch/utils/indexing.py` | Same, for the bulk path |
| `radis/pgsearch/tests/test_search_projection.py` | **New.** Columns, triggers, creation paths, backfill |
| `radis/pgsearch/tests/test_filter_query_equivalence.py` | **New.** Reference-oracle equivalence + plan shape |
| `docker-compose.base.yml` | Postgres tuning GUCs |
| `docs/user-docs/admin-guide.md` | Operator guidance for the GUCs |

---

### Task 1: Projection columns

**Files:**
- Modify: `radis/pgsearch/models.py`
- Create: `radis/pgsearch/migrations/0003_search_projection_columns.py`
- Test: `radis/pgsearch/tests/test_search_projection.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `ReportSearchIndex.group_ids: list[int]`, `.modality_codes: list[str]`, `.language_code: str | None`, `.patient_sex: str | None`, `.patient_age: int | None`, `.patient_id: str | None`, `.study_datetime: datetime | None`, `.study_description: str | None`, `.report_created_at: datetime | None`, `.report_updated_at: datetime | None`.

- [ ] **Step 1: Write the failing test**

Create `radis/pgsearch/tests/test_search_projection.py`:

```python
"""Tests for the ReportSearchIndex search projection.

The projection mirrors the Report fields that search filters on, so the FTS
candidate query can stay single-table. group_ids is access-control data, so
its correctness has its own tests here.
"""

import pytest
from adit_radis_shared.accounts.factories import GroupFactory

from radis.pgsearch.models import ReportSearchIndex
from radis.reports.factories import LanguageFactory, ReportFactory
from radis.reports.models import Report

pytestmark = pytest.mark.django_db


def test_new_index_row_defaults_to_empty_arrays():
    report = ReportFactory.create(language=LanguageFactory.create(code="en"))
    index = ReportSearchIndex.objects.get(report=report)

    assert index.group_ids == []
    assert index.modality_codes == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run cli test -- radis/pgsearch/tests/test_search_projection.py -v`
Expected: FAIL with `AttributeError: 'ReportSearchIndex' object has no attribute 'group_ids'`

- [ ] **Step 3: Add the fields to the model**

In `radis/pgsearch/models.py`, add to `ReportSearchIndex` after the `embedding` field:

```python
    # Search projection: mirrors of the Report fields the scan filters on, so
    # the FTS candidate query stays single-table. Maintained by the triggers in
    # migration 0004 and populated on creation by signals.py / indexing.py.
    # Nullable so every AddField stays metadata-only on a large table;
    # check_search_projection guards against drift instead of a NOT NULL scan.
    group_ids = ArrayField(models.IntegerField(), default=list)
    modality_codes = ArrayField(models.CharField(max_length=16), default=list)
    language_code = models.CharField(max_length=10, null=True)
    patient_sex = models.CharField(max_length=1, null=True)
    patient_age = models.IntegerField(null=True)
    patient_id = models.CharField(max_length=64, null=True)
    study_datetime = models.DateTimeField(null=True)
    study_description = models.CharField(max_length=64, blank=True, null=True)
    report_created_at = models.DateTimeField(null=True)
    report_updated_at = models.DateTimeField(null=True)
```

Add the import at the top of the file:

```python
from django.contrib.postgres.fields import ArrayField
```

- [ ] **Step 4: Generate the migration**

Run: `docker exec radis_dev-web-1 ./manage.py makemigrations pgsearch --name search_projection_columns`

Verify the generated `radis/pgsearch/migrations/0003_search_projection_columns.py` contains ten `AddField` operations and no `AlterField`. Every field must have either a constant default or `null=True` — that is what keeps each `AddField` metadata-only on an 8M-row table.

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run cli test -- radis/pgsearch/tests/test_search_projection.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add radis/pgsearch/models.py radis/pgsearch/migrations/0003_search_projection_columns.py radis/pgsearch/tests/test_search_projection.py
git commit -m "Add search projection columns to ReportSearchIndex"
```

---

### Task 2: Triggers that keep the projection true

**Files:**
- Create: `radis/pgsearch/migrations/0004_search_projection_triggers.py`
- Modify: `radis/pgsearch/tests/test_search_projection.py`

**Interfaces:**
- Consumes: the columns from Task 1.
- Produces: SQL functions `pgsearch_sync_group_ids()`, `pgsearch_sync_modality_codes()`, `pgsearch_sync_report_fields()`; triggers `pgsearch_group_ids_ins`/`_del`, `pgsearch_modality_codes_ins`/`_del`, `pgsearch_report_fields_upd`.

- [ ] **Step 1: Write the failing tests**

Append to `radis/pgsearch/tests/test_search_projection.py`:

```python
from django.db import connection


def test_adding_a_group_updates_the_projection():
    report = ReportFactory.create(language=LanguageFactory.create(code="en"))
    group = GroupFactory.create()

    report.groups.add(group)

    index = ReportSearchIndex.objects.get(report=report)
    assert index.group_ids == [group.pk]


def test_removing_a_group_updates_the_projection():
    """The leak direction: a report removed from a group must stop being visible."""
    report = ReportFactory.create(language=LanguageFactory.create(code="en"))
    group = GroupFactory.create()
    report.groups.add(group)

    report.groups.remove(group)

    index = ReportSearchIndex.objects.get(report=report)
    assert index.group_ids == []


def test_raw_sql_membership_write_updates_the_projection():
    """The whole reason for triggers over m2m_changed: writers that bypass the ORM."""
    report = ReportFactory.create(language=LanguageFactory.create(code="en"))
    group = GroupFactory.create()

    with connection.cursor() as cursor:
        cursor.execute(
            "INSERT INTO reports_report_groups (report_id, group_id) VALUES (%s, %s)",
            [report.pk, group.pk],
        )

    index = ReportSearchIndex.objects.get(report=report)
    assert index.group_ids == [group.pk]


def test_bulk_membership_insert_updates_every_affected_row():
    """Statement-level triggers fail classically by processing only one transition row.

    This is the shape reports/api/viewsets.py:229 uses for bulk upsert.
    """
    language = LanguageFactory.create(code="en")
    reports = [ReportFactory.create(language=language) for _ in range(5)]
    group = GroupFactory.create()

    through = Report.groups.through
    through.objects.bulk_create(
        [through(report_id=report.pk, group_id=group.pk) for report in reports]
    )

    for report in reports:
        index = ReportSearchIndex.objects.get(report=report)
        assert index.group_ids == [group.pk], f"report {report.pk} was not updated"


def test_deleting_a_report_does_not_error():
    """Deleting a Report cascades to both the membership rows and the index row,
    firing the membership trigger against a row that may already be gone. The
    spec calls that harmless in either cascade order; this pins it."""
    report = ReportFactory.create(language=LanguageFactory.create(code="en"))
    report.groups.add(GroupFactory.create())
    report_pk = report.pk

    report.delete()

    assert not ReportSearchIndex.objects.filter(report_id=report_pk).exists()


def test_updating_a_report_updates_the_mirrored_scalars():
    report = ReportFactory.create(language=LanguageFactory.create(code="en"))

    report.patient_id = "CHANGED-123"
    report.save()

    index = ReportSearchIndex.objects.get(report=report)
    assert index.patient_id == "CHANGED-123"


def test_patient_age_is_mirrored():
    """patient_age is a stored generated column. A BEFORE trigger would read NULL
    here; this test is what pins the triggers to AFTER."""
    report = ReportFactory.create(language=LanguageFactory.create(code="en"))

    report.patient_id = "TOUCH"
    report.save()

    index = ReportSearchIndex.objects.get(report=report)
    report.refresh_from_db()
    assert index.patient_age == report.patient_age
    assert index.patient_age is not None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run cli test -- radis/pgsearch/tests/test_search_projection.py -v`
Expected: the five new tests FAIL — `group_ids` stays `[]` and `patient_id` stays `None`, because nothing maintains them yet.

- [ ] **Step 3: Write the trigger migration**

Create `radis/pgsearch/migrations/0004_search_projection_triggers.py`:

```python
"""Statement-level triggers maintaining the ReportSearchIndex search projection.

Triggers rather than Django signals because group_ids is access-control data:
a signal fires only for ORM writes, missing management commands, the admin,
and raw SQL. A trigger holds inside the same transaction as the write, for
every writer.

Statement-level rather than row-level because the bulk-upsert endpoint writes
membership with bulk_create (reports/api/viewsets.py:215,229), so one statement
can carry a whole batch.

AFTER, never BEFORE: PostgreSQL does not expose stored generated column values
to BEFORE triggers, so patient_age would mirror as NULL.
"""

from django.db import migrations

SYNC_GROUP_IDS = """
CREATE OR REPLACE FUNCTION pgsearch_sync_group_ids() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    UPDATE pgsearch_reportsearchindex rsi
    SET group_ids = COALESCE(
        (SELECT array_agg(g.group_id ORDER BY g.group_id)
           FROM reports_report_groups g WHERE g.report_id = rsi.report_id),
        '{}')
    WHERE rsi.report_id IN (SELECT DISTINCT report_id FROM changed);
    RETURN NULL;
END $$;
"""

SYNC_MODALITY_CODES = """
CREATE OR REPLACE FUNCTION pgsearch_sync_modality_codes() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    UPDATE pgsearch_reportsearchindex rsi
    SET modality_codes = COALESCE(
        (SELECT array_agg(m.code ORDER BY m.code)
           FROM reports_report_modalities rm
           JOIN reports_modality m ON m.id = rm.modality_id
          WHERE rm.report_id = rsi.report_id),
        '{}')
    WHERE rsi.report_id IN (SELECT DISTINCT report_id FROM changed);
    RETURN NULL;
END $$;
"""

SYNC_REPORT_FIELDS = """
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

CREATE_TRIGGERS = """
CREATE TRIGGER pgsearch_group_ids_ins AFTER INSERT ON reports_report_groups
REFERENCING NEW TABLE AS changed
FOR EACH STATEMENT EXECUTE FUNCTION pgsearch_sync_group_ids();

CREATE TRIGGER pgsearch_group_ids_del AFTER DELETE ON reports_report_groups
REFERENCING OLD TABLE AS changed
FOR EACH STATEMENT EXECUTE FUNCTION pgsearch_sync_group_ids();

CREATE TRIGGER pgsearch_modality_codes_ins AFTER INSERT ON reports_report_modalities
REFERENCING NEW TABLE AS changed
FOR EACH STATEMENT EXECUTE FUNCTION pgsearch_sync_modality_codes();

CREATE TRIGGER pgsearch_modality_codes_del AFTER DELETE ON reports_report_modalities
REFERENCING OLD TABLE AS changed
FOR EACH STATEMENT EXECUTE FUNCTION pgsearch_sync_modality_codes();

CREATE TRIGGER pgsearch_report_fields_upd AFTER UPDATE ON reports_report
REFERENCING NEW TABLE AS changed
FOR EACH STATEMENT EXECUTE FUNCTION pgsearch_sync_report_fields();
"""

DROP_TRIGGERS = """
DROP TRIGGER IF EXISTS pgsearch_group_ids_ins ON reports_report_groups;
DROP TRIGGER IF EXISTS pgsearch_group_ids_del ON reports_report_groups;
DROP TRIGGER IF EXISTS pgsearch_modality_codes_ins ON reports_report_modalities;
DROP TRIGGER IF EXISTS pgsearch_modality_codes_del ON reports_report_modalities;
DROP TRIGGER IF EXISTS pgsearch_report_fields_upd ON reports_report;
DROP FUNCTION IF EXISTS pgsearch_sync_group_ids();
DROP FUNCTION IF EXISTS pgsearch_sync_modality_codes();
DROP FUNCTION IF EXISTS pgsearch_sync_report_fields();
"""


class Migration(migrations.Migration):
    dependencies = [
        ("pgsearch", "0003_search_projection_columns"),
        ("reports", "0013_alter_report_options"),
    ]

    operations = [
        migrations.RunSQL(SYNC_GROUP_IDS, reverse_sql=migrations.RunSQL.noop),
        migrations.RunSQL(SYNC_MODALITY_CODES, reverse_sql=migrations.RunSQL.noop),
        migrations.RunSQL(SYNC_REPORT_FIELDS, reverse_sql=migrations.RunSQL.noop),
        migrations.RunSQL(CREATE_TRIGGERS, reverse_sql=DROP_TRIGGERS),
    ]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run cli test -- radis/pgsearch/tests/test_search_projection.py -v`
Expected: PASS. If `test_patient_age_is_mirrored` fails with `None`, the triggers were written as BEFORE — fix, do not work around.

- [ ] **Step 5: Commit**

```bash
git add radis/pgsearch/migrations/0004_search_projection_triggers.py radis/pgsearch/tests/test_search_projection.py
git commit -m "Maintain the search projection with statement-level triggers"
```

---

### Task 3: Populate the projection when index rows are created

**Files:**
- Create: `radis/pgsearch/utils/projection.py`
- Modify: `radis/pgsearch/signals.py`
- Modify: `radis/pgsearch/utils/indexing.py`
- Modify: `radis/pgsearch/tests/test_search_projection.py`

**Interfaces:**
- Consumes: the columns from Task 1.
- Produces: `radis.pgsearch.utils.projection.sync_projection(report_ids: Iterable[int]) -> None` and the module constant `PROJECTION_UPDATE_SQL: str`.

The triggers maintain the projection on *change*, but a `ReportSearchIndex` row is created before its groups are attached (the API serializer calls `report.groups.set()` after `Report.objects.create()`). This task gives both creation paths one shared way to fill the row.

- [ ] **Step 1: Write the failing test**

Append to `radis/pgsearch/tests/test_search_projection.py`:

```python
from radis.pgsearch.utils.indexing import bulk_upsert_report_search_indexes


def test_creation_populates_the_mirrored_scalars():
    report = ReportFactory.create(language=LanguageFactory.create(code="de"))

    index = ReportSearchIndex.objects.get(report=report)
    assert index.language_code == "de"
    assert index.patient_id == report.patient_id


def test_bulk_upsert_populates_the_projection():
    language = LanguageFactory.create(code="en")
    report = ReportFactory.create(language=language, modalities=["CT"])
    group = GroupFactory.create()
    report.groups.add(group)
    ReportSearchIndex.objects.filter(report=report).delete()

    bulk_upsert_report_search_indexes([report.pk])

    index = ReportSearchIndex.objects.get(report=report)
    assert index.language_code == "en"
    assert index.group_ids == [group.pk]
    assert index.modality_codes == ["CT"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run cli test -- radis/pgsearch/tests/test_search_projection.py -v`
Expected: both FAIL — `language_code` is `None`.

- [ ] **Step 3: Write the shared projection helper**

Create `radis/pgsearch/utils/projection.py`:

```python
"""The canonical SQL that fills the ReportSearchIndex search projection.

Triggers (migration 0004) maintain the projection when its sources change.
This module fills it when an index row is first created, which happens before
the report's groups and modalities are attached. The backfill migration
duplicates this statement in SQL, the same way bulk_upsert_report_search_indexes
already duplicates the tsvector logic -- keep them in sync.
"""

from collections.abc import Iterable

from django.db import connection

PROJECTION_UPDATE_SQL = """
UPDATE pgsearch_reportsearchindex rsi
SET group_ids = COALESCE(g.ids, '{}'),
    modality_codes = COALESCE(m.codes, '{}'),
    language_code = l.code,
    patient_sex = r.patient_sex,
    patient_age = r.patient_age,
    patient_id = r.patient_id,
    study_datetime = r.study_datetime,
    study_description = r.study_description,
    report_created_at = r.created_at,
    report_updated_at = r.updated_at
FROM reports_report r
LEFT JOIN reports_language l ON l.id = r.language_id
LEFT JOIN LATERAL (
    SELECT array_agg(rg.group_id ORDER BY rg.group_id) AS ids
      FROM reports_report_groups rg WHERE rg.report_id = r.id
) g ON true
LEFT JOIN LATERAL (
    SELECT array_agg(mo.code ORDER BY mo.code) AS codes
      FROM reports_report_modalities rm
      JOIN reports_modality mo ON mo.id = rm.modality_id
     WHERE rm.report_id = r.id
) m ON true
WHERE rsi.report_id = r.id AND rsi.report_id = ANY(%s)
"""


def sync_projection(report_ids: Iterable[int]) -> None:
    """Fill the projection columns for the given reports from their sources."""
    ids = [int(report_id) for report_id in report_ids]
    if not ids:
        return
    with connection.cursor() as cursor:
        cursor.execute(PROJECTION_UPDATE_SQL, [ids])
```

- [ ] **Step 4: Call it from both creation paths**

In `radis/pgsearch/signals.py`, replace the body of the receiver:

```python
from django.db.models.signals import post_save
from django.dispatch import receiver

from radis.reports.models import Report

from .models import ReportSearchIndex
from .utils.projection import sync_projection


@receiver(post_save, sender=Report)
def create_or_update_report_search_index(sender, instance, created, **kwargs):
    if created:
        ReportSearchIndex.objects.create(report=instance)
        # Groups and modalities are attached after the Report is created, so
        # they stay empty here and the migration 0004 triggers fill them.
        sync_projection([instance.pk])
        return
    instance.search_index.save()
```

In `radis/pgsearch/utils/indexing.py`, add the import and call `sync_projection` once per chunk, immediately after the existing `cursor.execute(...)` block that sets `search_vector`:

```python
from .projection import sync_projection
```

```python
        sync_projection(chunk)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run cli test -- radis/pgsearch/tests/test_search_projection.py -v`
Expected: PASS

- [ ] **Step 6: Run the whole pgsearch suite for regressions**

Run: `uv run cli test -- radis/pgsearch -v`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add radis/pgsearch/utils/projection.py radis/pgsearch/signals.py radis/pgsearch/utils/indexing.py radis/pgsearch/tests/test_search_projection.py
git commit -m "Populate the search projection when index rows are created"
```

---

### Task 4: Backfill existing rows

**Files:**
- Create: `radis/pgsearch/migrations/0005_search_projection_backfill.py`
- Modify: `radis/pgsearch/tests/test_search_projection.py`

**Interfaces:**
- Consumes: `PROJECTION_UPDATE_SQL` shape from Task 3 (duplicated inline — migrations stay self-contained).
- Produces: nothing importable.

Measured at 8M: **9 minutes 20 seconds**, in 50,000-row chunks. This blocks the deploy, which is accepted for the target deployment.

- [ ] **Step 1: Write the failing test**

Append to `radis/pgsearch/tests/test_search_projection.py`:

```python
def test_backfill_fills_rows_that_predate_the_projection():
    """Simulates a row written before the projection existed."""
    language = LanguageFactory.create(code="en")
    report = ReportFactory.create(language=language, modalities=["MR"])
    group = GroupFactory.create()
    report.groups.add(group)

    ReportSearchIndex.objects.filter(report=report).update(
        group_ids=[], modality_codes=[], language_code=None, patient_id=None
    )

    from radis.pgsearch.utils.projection import sync_projection

    sync_projection([report.pk])

    index = ReportSearchIndex.objects.get(report=report)
    assert index.group_ids == [group.pk]
    assert index.modality_codes == ["MR"]
    assert index.language_code == "en"
    assert index.patient_id == report.patient_id
```

- [ ] **Step 2: Run test to verify it passes already**

Run: `uv run cli test -- radis/pgsearch/tests/test_search_projection.py::test_backfill_fills_rows_that_predate_the_projection -v`
Expected: PASS — Task 3 already provides the mechanism. This test pins the *semantics* the migration depends on, so a later refactor of `PROJECTION_UPDATE_SQL` cannot silently break the backfill.

- [ ] **Step 3: Write the backfill migration**

Create `radis/pgsearch/migrations/0005_search_projection_backfill.py`:

```python
"""Backfill the ReportSearchIndex search projection for existing rows.

atomic = False so each chunk commits on its own: at the 8M design target this
runs for about nine and a half minutes, and one transaction that long would
pin an equally long-lived snapshot.

Runs after the triggers (0004) on purpose. A report edited during the backfill
is corrected by its trigger, and a chunk that later reprocesses the same row
simply rewrites the current values.

The statement below duplicates utils/projection.PROJECTION_UPDATE_SQL, the same
way bulk_upsert_report_search_indexes duplicates the tsvector logic. Migrations
must not import app code, which drifts under them.
"""

from django.db import migrations

CHUNK_SIZE = 50_000

BACKFILL_SQL = """
UPDATE pgsearch_reportsearchindex rsi
SET group_ids = COALESCE(g.ids, '{}'),
    modality_codes = COALESCE(m.codes, '{}'),
    language_code = l.code,
    patient_sex = r.patient_sex,
    patient_age = r.patient_age,
    patient_id = r.patient_id,
    study_datetime = r.study_datetime,
    study_description = r.study_description,
    report_created_at = r.created_at,
    report_updated_at = r.updated_at
FROM reports_report r
LEFT JOIN reports_language l ON l.id = r.language_id
LEFT JOIN LATERAL (
    SELECT array_agg(rg.group_id ORDER BY rg.group_id) AS ids
      FROM reports_report_groups rg WHERE rg.report_id = r.id
) g ON true
LEFT JOIN LATERAL (
    SELECT array_agg(mo.code ORDER BY mo.code) AS codes
      FROM reports_report_modalities rm
      JOIN reports_modality mo ON mo.id = rm.modality_id
     WHERE rm.report_id = r.id
) m ON true
WHERE rsi.report_id = r.id AND rsi.report_id > %s AND rsi.report_id <= %s
"""


def backfill(apps, schema_editor):
    connection = schema_editor.connection
    with connection.cursor() as cursor:
        cursor.execute("SELECT COALESCE(MAX(report_id), 0) FROM pgsearch_reportsearchindex")
        max_id = cursor.fetchone()[0]

    low = 0
    while low < max_id:
        high = low + CHUNK_SIZE
        with connection.cursor() as cursor:
            cursor.execute(BACKFILL_SQL, [low, high])
        low = high


class Migration(migrations.Migration):
    atomic = False

    dependencies = [("pgsearch", "0004_search_projection_triggers")]

    operations = [migrations.RunPython(backfill, migrations.RunPython.noop)]
```

- [ ] **Step 4: Verify the migration applies cleanly**

Run: `uv run cli test -- radis/pgsearch -v`
Expected: PASS — the test database is built by applying every migration, so a broken migration fails the whole suite.

- [ ] **Step 5: Commit**

```bash
git add radis/pgsearch/migrations/0005_search_projection_backfill.py radis/pgsearch/tests/test_search_projection.py
git commit -m "Backfill the search projection in 50k-row chunks"
```

---

### Task 5: Indexes, created last

**Files:**
- Modify: `radis/pgsearch/models.py`
- Create: `radis/pgsearch/migrations/0006_search_projection_indexes.py`

**Interfaces:**
- Consumes: the columns from Task 1.
- Produces: indexes `pgsearch_group_ids_gin`, `pgsearch_report_updated_at_idx`.

Created after the backfill deliberately: while the new columns are unindexed the backfill can use HOT updates. Measured at 8M — GIN 3.7 s, btree 1.6 s. Two indexes only; `modality_codes`, `study_datetime`, `patient_id` and `report_created_at` are deliberately unindexed (spec §4.1).

- [ ] **Step 1: Add the indexes to the model**

In `radis/pgsearch/models.py`, add to `ReportSearchIndex.Meta.indexes`:

```python
            GinIndex(fields=["group_ids"], name="pgsearch_group_ids_gin"),
            models.Index(fields=["report_updated_at"], name="pgsearch_report_updated_at_idx"),
```

- [ ] **Step 2: Generate the migration**

Run: `docker exec radis_dev-web-1 ./manage.py makemigrations pgsearch --name search_projection_indexes`

- [ ] **Step 3: Add the GIN fastupdate change**

Append to the `operations` list in the generated `0006_search_projection_indexes.py`:

```python
        migrations.RunSQL(
            # fastupdate leaves an unsorted pending list that every query must
            # scan; RADIS writes in async batches and reads interactively, so
            # predictable read latency is the better side of the trade.
            "ALTER INDEX pgsearch_re_search__b0f715_gin SET (fastupdate = off);"
            "SELECT gin_clean_pending_list('pgsearch_re_search__b0f715_gin');",
            reverse_sql="ALTER INDEX pgsearch_re_search__b0f715_gin SET (fastupdate = on);",
        ),
```

- [ ] **Step 4: Write the test**

Append to `radis/pgsearch/tests/test_search_projection.py`:

```python
def test_projection_indexes_exist():
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT indexname FROM pg_indexes WHERE tablename = 'pgsearch_reportsearchindex'"
        )
        names = {row[0] for row in cursor.fetchall()}

    assert "pgsearch_group_ids_gin" in names
    assert "pgsearch_report_updated_at_idx" in names
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run cli test -- radis/pgsearch/tests/test_search_projection.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add radis/pgsearch/models.py radis/pgsearch/migrations/0006_search_projection_indexes.py radis/pgsearch/tests/test_search_projection.py
git commit -m "Add the search projection indexes and disable GIN fastupdate"
```

---

### Task 6: check_search_projection command

**Files:**
- Create: `radis/pgsearch/management/commands/check_search_projection.py`
- Create: `radis/pgsearch/tests/test_check_search_projection.py`

**Interfaces:**
- Consumes: the columns from Task 1.
- Produces: management command `check_search_projection`, exiting non-zero when drift is found.

- [ ] **Step 1: Write the failing test**

Create `radis/pgsearch/tests/test_check_search_projection.py`:

```python
"""The projection duplicates access-control data, so operators need a way to
prove it still matches its sources -- after a restore, a bulk import, or a
Language.code rename, which no trigger covers."""

import pytest
from adit_radis_shared.accounts.factories import GroupFactory
from django.core.management import call_command
from django.core.management.base import CommandError

from radis.pgsearch.models import ReportSearchIndex
from radis.reports.factories import LanguageFactory, ReportFactory

pytestmark = pytest.mark.django_db


def test_reports_no_drift_on_a_healthy_corpus():
    report = ReportFactory.create(language=LanguageFactory.create(code="en"))
    report.groups.add(GroupFactory.create())

    call_command("check_search_projection")


def test_detects_drifted_group_ids():
    report = ReportFactory.create(language=LanguageFactory.create(code="en"))
    report.groups.add(GroupFactory.create())

    ReportSearchIndex.objects.filter(report=report).update(group_ids=[9999])

    with pytest.raises(CommandError, match="group_ids"):
        call_command("check_search_projection")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run cli test -- radis/pgsearch/tests/test_check_search_projection.py -v`
Expected: FAIL with `Unknown command: 'check_search_projection'`

- [ ] **Step 3: Write the command**

Create `radis/pgsearch/management/commands/check_search_projection.py`:

```python
"""Verify the ReportSearchIndex search projection against its sources."""

from django.core.management.base import BaseCommand, CommandError
from django.db import connection

DRIFT_SQL = """
SELECT
    count(*) FILTER (WHERE rsi.group_ids IS DISTINCT FROM COALESCE(g.ids, '{}'))
        AS group_ids,
    count(*) FILTER (WHERE rsi.modality_codes IS DISTINCT FROM COALESCE(m.codes, '{}'))
        AS modality_codes,
    count(*) FILTER (WHERE rsi.language_code IS DISTINCT FROM l.code)
        AS language_code,
    count(*) FILTER (WHERE rsi.patient_sex IS DISTINCT FROM r.patient_sex)
        AS patient_sex,
    count(*) FILTER (WHERE rsi.patient_age IS DISTINCT FROM r.patient_age)
        AS patient_age,
    count(*) FILTER (WHERE rsi.patient_id IS DISTINCT FROM r.patient_id)
        AS patient_id,
    count(*) FILTER (WHERE rsi.study_datetime IS DISTINCT FROM r.study_datetime)
        AS study_datetime,
    count(*) FILTER (WHERE rsi.study_description IS DISTINCT FROM r.study_description)
        AS study_description,
    count(*) FILTER (WHERE rsi.report_created_at IS DISTINCT FROM r.created_at)
        AS report_created_at,
    count(*) FILTER (WHERE rsi.report_updated_at IS DISTINCT FROM r.updated_at)
        AS report_updated_at
FROM pgsearch_reportsearchindex rsi
JOIN reports_report r ON r.id = rsi.report_id
LEFT JOIN reports_language l ON l.id = r.language_id
LEFT JOIN LATERAL (
    SELECT array_agg(rg.group_id ORDER BY rg.group_id) AS ids
      FROM reports_report_groups rg WHERE rg.report_id = r.id
) g ON true
LEFT JOIN LATERAL (
    SELECT array_agg(mo.code ORDER BY mo.code) AS codes
      FROM reports_report_modalities rm
      JOIN reports_modality mo ON mo.id = rm.modality_id
     WHERE rm.report_id = r.id
) m ON true
"""


class Command(BaseCommand):
    help = "Verify the ReportSearchIndex search projection against its sources."

    def handle(self, *args, **options):
        with connection.cursor() as cursor:
            cursor.execute(DRIFT_SQL)
            columns = [column.name for column in cursor.description]
            counts = dict(zip(columns, cursor.fetchone(), strict=True))

        drifted = {name: count for name, count in counts.items() if count}
        if drifted:
            details = ", ".join(f"{name}: {count}" for name, count in sorted(drifted.items()))
            raise CommandError(f"Search projection drift detected -- {details}")

        self.stdout.write(self.style.SUCCESS("Search projection matches its sources."))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run cli test -- radis/pgsearch/tests/test_check_search_projection.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add radis/pgsearch/management/commands/check_search_projection.py radis/pgsearch/tests/test_check_search_projection.py
git commit -m "Add check_search_projection drift detection command"
```

---

### Task 7: Single-table _build_filter_query

**Files:**
- Modify: `radis/pgsearch/providers.py:180-244`
- Create: `radis/pgsearch/tests/test_filter_query_equivalence.py`

**Interfaces:**
- Consumes: the projection columns from Task 1, populated by Tasks 2–4.
- Produces: `_build_filter_query(filters: SearchFilters) -> Q` returning predicates against `ReportSearchIndex`'s own columns only, with no `report__` traversal.

This is the task that delivers the performance change. It is also the one that can leak reports between groups, so the equivalence test comes first.

- [ ] **Step 1: Write the failing equivalence test**

Create `radis/pgsearch/tests/test_filter_query_equivalence.py`:

```python
"""The new single-table filter must select exactly what the joined one did.

_build_filter_query_legacy is the pre-change implementation kept as a reference
oracle. Delete both it and this module once the change has settled in production.
"""

from datetime import date, timedelta

import pytest
from adit_radis_shared.accounts.factories import GroupFactory
from django.db.models import Q
from django.utils import timezone

from radis.pgsearch.models import ReportSearchIndex
from radis.pgsearch.providers import _build_filter_query
from radis.reports.factories import LanguageFactory, ReportFactory
from radis.search.site import SearchFilters

pytestmark = pytest.mark.django_db


def _build_filter_query_legacy(filters: SearchFilters) -> Q:
    """The joined implementation this change replaces."""
    fq = Q(report__groups=filters.group)
    if filters.patient_sex:
        fq &= Q(report__patient_sex=filters.patient_sex)
    if filters.language:
        fq &= Q(report__language__code=filters.language)
    if filters.modalities:
        fq &= Q(report__modalities__code__in=filters.modalities)
    if filters.study_date_from:
        fq &= Q(report__study_datetime__date__gte=filters.study_date_from)
    if filters.study_date_till:
        fq &= Q(report__study_datetime__date__lte=filters.study_date_till)
    if filters.study_description:
        fq &= Q(report__study_description__icontains=filters.study_description)
    if filters.patient_age_from is not None:
        fq &= Q(report__patient_age__gte=filters.patient_age_from)
    if filters.patient_age_till is not None:
        fq &= Q(report__patient_age__lte=filters.patient_age_till)
    if filters.patient_id:
        fq &= Q(report__patient_id=filters.patient_id)
    if filters.updated_after:
        fq &= Q(report__updated_at__gte=filters.updated_after)
    return fq


def _ids(fq: Q) -> set[int]:
    return set(
        ReportSearchIndex.objects.filter(fq).distinct().values_list("report_id", flat=True)
    )


@pytest.fixture
def corpus():
    language_en = LanguageFactory.create(code="en")
    language_de = LanguageFactory.create(code="de")
    group_a = GroupFactory.create(name="A")
    group_b = GroupFactory.create(name="B")
    now = timezone.now()

    first = ReportFactory.create(
        language=language_en, patient_sex="M", patient_id="P1",
        study_description="CT Thorax", study_datetime=now - timedelta(days=10),
        modalities=["CT"],
    )
    first.groups.add(group_a)

    second = ReportFactory.create(
        language=language_de, patient_sex="F", patient_id="P2",
        study_description="MR Head", study_datetime=now - timedelta(days=400),
        modalities=["MR", "CT"],
    )
    second.groups.add(group_a, group_b)

    orphan = ReportFactory.create(language=language_en, patient_sex="M", patient_id="P3")

    return {"group_a": group_a, "group_b": group_b, "orphan": orphan}


@pytest.mark.parametrize(
    "make_filters",
    [
        pytest.param(lambda c: SearchFilters(group=c["group_a"].pk), id="group-only"),
        pytest.param(lambda c: SearchFilters(group=c["group_b"].pk), id="other-group"),
        pytest.param(
            lambda c: SearchFilters(group=c["group_a"].pk, language="en"), id="language"
        ),
        pytest.param(
            lambda c: SearchFilters(group=c["group_a"].pk, modalities=["CT"]), id="modality"
        ),
        pytest.param(
            lambda c: SearchFilters(group=c["group_a"].pk, modalities=["CT", "MR"]),
            id="modalities-multi",
        ),
        pytest.param(
            lambda c: SearchFilters(group=c["group_a"].pk, patient_sex="M"), id="sex"
        ),
        pytest.param(
            lambda c: SearchFilters(group=c["group_a"].pk, patient_id="P1"), id="patient-id"
        ),
        pytest.param(
            lambda c: SearchFilters(group=c["group_a"].pk, study_description="thorax"),
            id="description-icontains",
        ),
        pytest.param(
            lambda c: SearchFilters(
                group=c["group_a"].pk, study_date_from=date.today() - timedelta(days=30)
            ),
            id="date-from",
        ),
        pytest.param(
            lambda c: SearchFilters(
                group=c["group_a"].pk, study_date_till=date.today() - timedelta(days=5)
            ),
            id="date-till",
        ),
        pytest.param(
            lambda c: SearchFilters(
                group=c["group_a"].pk,
                study_date_from=date.today() - timedelta(days=30),
                study_date_till=date.today(),
            ),
            id="date-range",
        ),
        pytest.param(
            lambda c: SearchFilters(group=c["group_a"].pk, patient_age_from=0), id="age-from"
        ),
        pytest.param(
            lambda c: SearchFilters(group=c["group_a"].pk, patient_age_till=200), id="age-till"
        ),
        pytest.param(
            lambda c: SearchFilters(
                group=c["group_a"].pk, updated_after=timezone.now() - timedelta(hours=1)
            ),
            id="updated-after",
        ),
    ],
)
def test_new_filter_matches_the_legacy_one(corpus, make_filters):
    filters = make_filters(corpus)

    assert _ids(_build_filter_query(filters)) == _ids(_build_filter_query_legacy(filters))


def test_group_none_is_fail_closed(corpus):
    """group=None must match only reports in no group at all -- never everything."""
    result = _ids(_build_filter_query(SearchFilters(group=None)))

    assert result == {corpus["orphan"].pk}


def test_no_duplicates_without_distinct(corpus):
    """A report matching on two modalities must appear once, which is what the
    removed .distinct() used to guarantee."""
    filters = SearchFilters(group=corpus["group_a"].pk, modalities=["CT", "MR"])

    rows = list(
        ReportSearchIndex.objects.filter(_build_filter_query(filters)).values_list(
            "report_id", flat=True
        )
    )

    assert len(rows) == len(set(rows))
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run cli test -- radis/pgsearch/tests/test_filter_query_equivalence.py -v`
Expected: `test_no_duplicates_without_distinct` FAILS (the current implementation duplicates), and `test_group_none_is_fail_closed` may pass incidentally. The parametrised cases pass trivially because both sides are still the same code.

- [ ] **Step 3: Rewrite _build_filter_query**

In `radis/pgsearch/providers.py`, replace the whole `_build_filter_query` function with:

```python
def _local_day_start(day: date) -> datetime:
    """Midnight of ``day`` in the active timezone, as an aware datetime."""
    return timezone.make_aware(datetime.combine(day, time.min))


def _build_filter_query(filters: SearchFilters) -> Q:
    # Every predicate here is a column on ReportSearchIndex itself -- the search
    # projection (see migration 0003) mirrors the Report fields search filters
    # on, so the candidate query stays single-table. Reintroducing a ``report__``
    # traversal restores the join and the multi-second query shape it caused;
    # test_filter_query_plan_is_single_table guards against that.
    #
    # Group-scoped access control. ``SearchView`` supplies
    # ``group=active_group.pk``; the extraction preview may pass ``group=None``
    # when the user has no active group, which is fail-closed: an empty
    # ``group_ids`` matches only reports assigned to no group at all.
    if filters.group is None:
        fq = Q(group_ids=[])
    else:
        fq = Q(group_ids__contains=[filters.group])

    # Apply hard filter criteria
    if filters.patient_sex:
        fq &= Q(patient_sex=filters.patient_sex)
    if filters.language:
        fq &= Q(language_code=filters.language)
    if filters.modalities:
        fq &= Q(modality_codes__overlap=filters.modalities)
    # Half-open ranges rather than ``__date``: Django compiles that lookup to
    # ``(study_datetime AT TIME ZONE ...)::date``, a function over the column
    # evaluated once per row -- millions of timezone conversions per query on
    # the sequential scan, for a predicate equal to two timestamp comparisons.
    if filters.study_date_from:
        fq &= Q(study_datetime__gte=_local_day_start(filters.study_date_from))
    if filters.study_date_till:
        fq &= Q(study_datetime__lt=_local_day_start(filters.study_date_till + timedelta(days=1)))
    if filters.study_description:
        fq &= Q(study_description__icontains=filters.study_description)
    if filters.patient_age_from is not None:
        fq &= Q(patient_age__gte=filters.patient_age_from)
    if filters.patient_age_till is not None:
        fq &= Q(patient_age__lte=filters.patient_age_till)
    if filters.patient_id:
        fq &= Q(patient_id=filters.patient_id)
    if filters.created_after:
        fq &= Q(report_created_at__gte=filters.created_after)
    if filters.created_before:
        fq &= Q(report_created_at__lte=filters.created_before)
    if filters.labels:
        from radis.labels.models import LabelResult
        from radis.reports.models import Report

        surfacing_report_ids = Report.objects.filter(
            label_results__label__name__in=filters.labels,
            label_results__value__in=LabelResult.SURFACING_VALUES,
        ).values("pk")
        fq &= Q(report_id__in=surfacing_report_ids)
    if filters.updated_after:
        fq &= Q(report_updated_at__gte=filters.updated_after)

    return fq
```

Add these imports at the top of `providers.py`:

```python
from datetime import date, datetime, time, timedelta

from django.utils import timezone
```

- [ ] **Step 4: Remove the two `.distinct()` calls**

In `_fuse_hybrid`, delete `.distinct()` from both the vector queryset (`providers.py:394`) and the FTS queryset (`providers.py:412`). With no joins there is nothing to deduplicate.

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run cli test -- radis/pgsearch/tests/test_filter_query_equivalence.py -v`
Expected: PASS, all parametrised cases plus both named tests.

- [ ] **Step 6: Run the full suite for regressions**

Run: `uv run cli test -- radis/pgsearch radis/search radis/extractions radis/subscriptions -v`
Expected: PASS. These are the four consumers of `_build_filter_query`.

- [ ] **Step 7: Commit**

```bash
git add radis/pgsearch/providers.py radis/pgsearch/tests/test_filter_query_equivalence.py
git commit -m "Make the FTS filter query single-table"
```

---

### Task 8: Language predicates off the join

**Files:**
- Modify: `radis/pgsearch/providers.py` — `_exclude_negations`, `_fuse_hybrid`, `search`

**Interfaces:**
- Consumes: `ReportSearchIndex.language_code` from Task 1.
- Produces: no signature change.

Five `report__language__code__in` traversals remain after Task 7 and would each re-add the `reports_language` join.

- [ ] **Step 1: Write the failing test**

Append to `radis/pgsearch/tests/test_filter_query_equivalence.py`:

```python
def test_no_report_traversal_remains_in_the_candidate_query():
    """Any report__ lookup re-adds a join and the multi-second query shape."""
    import inspect

    from radis.pgsearch import providers

    source = inspect.getsource(providers)
    offending = [
        line.strip()
        for line in source.splitlines()
        if "report__" in line
        and "report__body" not in line  # hydration headline, deliberately joined
        and "report__document_id" not in line  # filter(), deliberately joined
        and not line.strip().startswith("#")
    ]

    assert offending == [], f"report__ traversals left in the hot path: {offending}"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run cli test -- radis/pgsearch/tests/test_filter_query_equivalence.py::test_no_report_traversal_remains_in_the_candidate_query -v`
Expected: FAIL listing the five `report__language__code__in` lines.

- [ ] **Step 3: Replace the traversals**

In `radis/pgsearch/providers.py`, replace every `report__language__code__in=codes` with `language_code__in=codes`. There are five occurrences:

- `_exclude_negations`, in the `queryset.exclude(...)` call
- `_fuse_hybrid`, building `match_q`
- `_fuse_hybrid`, building `rank_expr`
- `search`, building `summary_expr`
- `search`, building its own `rank_expr`

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run cli test -- radis/pgsearch -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add radis/pgsearch/providers.py radis/pgsearch/tests/test_filter_query_equivalence.py
git commit -m "Read language_code from the projection instead of joining"
```

---

### Task 9: Plan-shape regression test

**Files:**
- Modify: `radis/pgsearch/tests/test_filter_query_equivalence.py`

**Interfaces:**
- Consumes: everything from Tasks 1–8.
- Produces: nothing importable.

Wall-clock assertions are flaky in CI; plan structure is not. This is what catches a future change silently restoring the 8-second query.

- [ ] **Step 1: Write the failing test**

Append to `radis/pgsearch/tests/test_filter_query_equivalence.py`:

```python
def test_filter_query_plan_is_single_table(corpus):
    """The candidate query must not join the membership table or deduplicate."""
    from django.db import connection

    filters = SearchFilters(group=corpus["group_a"].pk, language="en")
    queryset = ReportSearchIndex.objects.filter(_build_filter_query(filters)).values_list(
        "report_id", flat=True
    )
    sql, params = queryset.query.sql_with_params()

    with connection.cursor() as cursor:
        cursor.execute(f"EXPLAIN {sql}", params)
        plan = "\n".join(row[0] for row in cursor.fetchall())

    assert "reports_report_groups" not in plan, plan
    assert "Unique" not in plan, plan
```

- [ ] **Step 2: Run test to verify it passes**

Run: `uv run cli test -- radis/pgsearch/tests/test_filter_query_equivalence.py::test_filter_query_plan_is_single_table -v`
Expected: PASS — Tasks 7 and 8 already removed the join. If it fails, one of them is incomplete.

- [ ] **Step 3: Commit**

```bash
git add radis/pgsearch/tests/test_filter_query_equivalence.py
git commit -m "Assert the candidate query plan stays single-table"
```

---

### Task 10: PostgreSQL tuning and operator documentation

**Files:**
- Modify: `docker-compose.base.yml`
- Modify: `docs/user-docs/admin-guide.md`
- Modify: `CLAUDE.md`

**Interfaces:**
- Consumes: nothing.
- Produces: env vars `POSTGRES_MAX_PARALLEL_WORKERS_PER_GATHER`, `POSTGRES_MAX_PARALLEL_WORKERS`, `POSTGRES_MAX_WORKER_PROCESSES`, `POSTGRES_SHARED_BUFFERS`.

These are compose-only overrides with defaults, **not** `example.env` entries — matching how the project already treats `EMBEDDINGS_WORKER_CONCURRENCY` and `WAIT_POSTGRES_TIMEOUT`. Only the first differs from PostgreSQL's own default.

- [ ] **Step 1: Add the GUCs to the compose file**

In `docker-compose.base.yml`, replace the `postgres` service with:

```yaml
  postgres:
    image: pgvector/pgvector:pg17@sha256:cf134a767f474095eeba57e0117be8e568e011a63f33fbf252f14c9b760f8e6f
    hostname: postgres.local
    command:
      - postgres
      - -c
      - max_parallel_workers_per_gather=${POSTGRES_MAX_PARALLEL_WORKERS_PER_GATHER:-4}
      - -c
      - max_parallel_workers=${POSTGRES_MAX_PARALLEL_WORKERS:-8}
      - -c
      - max_worker_processes=${POSTGRES_MAX_WORKER_PROCESSES:-8}
      - -c
      - shared_buffers=${POSTGRES_SHARED_BUFFERS:-128MB}
    volumes:
      - postgres_data:/var/lib/postgresql/data
```

- [ ] **Step 2: Verify the stack starts and the setting took effect**

```bash
uv run cli compose-up -- -d
docker exec radis_dev-postgres-1 psql -U postgres -c "SHOW max_parallel_workers_per_gather;"
```

Expected: `4`

- [ ] **Step 3: Document them for operators**

Add to `docs/user-docs/admin-guide.md`:

```markdown
## Database tuning

Search scans the report index table in parallel, so the number of parallel
workers is the one PostgreSQL setting worth revisiting. These are optional
overrides -- set them in `.env` only if the defaults do not suit your hardware.

| Variable | Default | Guidance |
| --- | --- | --- |
| `POSTGRES_MAX_PARALLEL_WORKERS_PER_GATHER` | `4` | Measured on 8M reports: 606 ms at 2, 421 ms at 4, 343 ms at 8. Four captures most of the gain while leaving cores for concurrent searches. |
| `POSTGRES_MAX_PARALLEL_WORKERS` | `8` | PostgreSQL's default. Raise together with the two others on a larger host. |
| `POSTGRES_MAX_WORKER_PROCESSES` | `8` | As above. |
| `POSTGRES_SHARED_BUFFERS` | `128MB` | PostgreSQL's default. Around 25% of host RAM is the usual recommendation; a value larger than the container's memory will prevent PostgreSQL from starting. |
```

Add the same four variables to the environment-variable section of `CLAUDE.md`, noting they are compose-only overrides.

- [ ] **Step 4: Commit**

```bash
git add docker-compose.base.yml docs/user-docs/admin-guide.md CLAUDE.md
git commit -m "Tune PostgreSQL parallelism for the search scan"
```

---

## Verification after all tasks

- [ ] `uv run cli test` — full suite green
- [ ] `uv run cli lint` — clean
- [ ] `docker exec radis_dev-web-1 ./manage.py check_search_projection` — reports no drift
- [ ] Search the dev stack for a common term and confirm results still render
- [ ] **Manual, against a large corpus:** confirm the spec's success criterion —
      the FTS candidate query under 1,000 ms at the 8M design target with
      `max_parallel_workers_per_gather = 4`. Not assertable in CI, which has no
      corpus of that size; the plan-shape test in Task 9 is the CI proxy.
