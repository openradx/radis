# Auto-Labeling — Plan 2: Execution Paths + Admin Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Drive the Plan 1 `label_report()` engine automatically — via a periodic incremental scan of newly-ingested reports and an admin-triggered full-corpus backfill — both flowing through the existing `AnalysisJob`/`AnalysisTask` machinery, with an admin cockpit to author labels and run/monitor backfills.

**Architecture:** `LabelingJob` (subclasses `AnalysisJob`) and `LabelingTask` (subclasses `AnalysisTask`) reuse the Job→Task processing model. A `trigger` field distinguishes `SCAN` (recent window) from `MANUAL` (full corpus). `process_labeling_job` (default queue) streams reports into batched tasks during PREPARING; `process_labeling_task` (llm queue) runs `label_report` per report under a thread pool. A Procrastinate `@app.periodic` task advances a singleton checkpoint and creates SCAN jobs. A DB-level partial unique index enforces "one active LabelingJob at a time."

**Tech Stack:** Python 3.12, Django 6.0, PostgreSQL 17, Procrastinate, pytest/pytest-django, factory-boy.

**Source spec:** `docs/superpowers/specs/2026-05-21-auto-labeling-design.md` (sections "Execution Path 1", "Execution Path 2", "Admin UX"). Plan 1 (`...-1-foundation.md`) is a prerequisite and is complete.

---

## Prerequisites and carry-forward from Plan 1

Plan 1 delivered: the `radis.labels` app, the five models (`LabelGroup`, `Label`, `LabelResult`, `GateAnswer`, `LabelingScanCheckpoint`), schema/prompt builders, and `label_report(report_id)` in `radis/labels/labeling.py`. 35 tests pass; ruff + pyright clean.

**Environment notes (carried from Plan 1 execution):**

- **Django is 6.0.1**, not 5.1. `CheckConstraint` uses `condition=` (not `check=`).
- **Running tests:** do NOT use `uv run cli test` (it gates on dev containers). A standalone Postgres on `localhost:5432` (postgres/postgres) suffices. Run: `DJANGO_SETTINGS_MODULE=radis.settings.development uv run pytest <path> -p no:cacheprovider -q`. If no Postgres is running: `docker run -d --name radis-test-pg -e POSTGRES_PASSWORD=postgres -p 5432:5432 postgres:17`.
- **Commits:** prefix `git commit` with `PRE_COMMIT_ALLOW_NO_CONFIG=1`. End message bodies with `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`.
- **Code standards enforced:** ruff (`uv run ruff check radis/labels/`), format (`uv run ruff format`), and **pyright basic** (`uv run pyright radis/labels/`) — the project keeps test files pyright-clean too. Annotate accessed FK ids on models (`id: int`, `<fk>_id: int`) and type `create_model` field dicts as `dict[str, Any]` (the established conventions). Use `.pk` not `.id` on `Report` (foreign model) in tests.

**Three base-class reconciliations (the spec's pseudocode glosses these):**

1. `AnalysisJob.owner` (in `radis/core/models.py`) is a **non-nullable** FK to the user model. Scan-created jobs have no human owner. **Resolution:** override `owner` on `LabelingJob` as nullable (`null=True, blank=True`); scan jobs leave it null, manual jobs set it to the triggering admin.
2. The abstract `AnalysisJob`/`AnalysisTask` do **not** declare a `queued_job` field — `ExtractionJob`/`ExtractionTask` each add their own. `LabelingJob.delay()`/`LabelingTask.delay()` rely on `queued_job_id`, so both must declare the `queued_job` `OneToOneField(ProcrastinateJob, null=True, on_delete=SET_NULL, related_name="+")` and the `queued_job_id: int | None` annotation. Mirror `radis/extractions/models.py`.
3. `process_extraction_job` enforces a strict invariant: **never enqueue tasks while PREPARING**; only after switching to PENDING. `process_labeling_job` must mirror this (see `radis/extractions/tasks.py`).

---

## File Structure (Plan 2)

**Create:**
- `radis/labels/tasks.py` — `process_labeling_job`, `process_labeling_task`, `incremental_label_scan`, task-creation helpers.
- `radis/labels/processors.py` — `LabelingTaskProcessor(AnalysisTaskProcessor)`.
- `radis/labels/scope.py` — `_needs_work_queryset` backfill scope query.
- `radis/labels/admin.py` — all admin registrations + the backfill cockpit.
- `radis/labels/migrations/0002_*.py` — generated, plus a `RunSQL` partial unique index for the singleton.
- `radis/labels/tests/test_jobs.py`, `test_scope.py`, `test_processor.py`, `test_scan.py`, `test_admin.py`.

**Modify:**
- `radis/labels/models.py` — add `LabelingJob`, `LabelingTask`.
- `radis/labels/factories.py` — add `LabelingJobFactory`, `LabelingTaskFactory`.
- `radis/reports/admin.py` — add `LabelResultInline` to `ReportAdmin.inlines`.
- `radis/labels/apps.py` — import `tasks` in `ready()` so Procrastinate registers the periodic task (mirror how `radis.subscriptions` does it, or confirm Procrastinate autodiscovery).

---

## Task 1: `LabelingJob` and `LabelingTask` models + singleton index

**Files:**
- Modify: `radis/labels/models.py`
- Modify: `radis/labels/factories.py`
- Create: `radis/labels/migrations/0002_labelingjob_labelingtask.py` (generated + edited to add RunSQL index)
- Test: `radis/labels/tests/test_jobs.py`

- [ ] **Step 1: Add the models** to `radis/labels/models.py`. Append after the existing models. Reference `radis/extractions/models.py` for the `queued_job`/`delay()` pattern and `radis/core/models.py` for `AnalysisJob`/`AnalysisTask`.

```python
# --- add these imports at the top of models.py ---
from django.conf import settings
from django.urls import reverse
from procrastinate.contrib.django import app
from procrastinate.contrib.django.models import ProcrastinateJob

from radis.core.models import AnalysisJob, AnalysisTask


# --- append at the end of models.py ---
class LabelingJob(AnalysisJob):
    class Trigger(models.TextChoices):
        SCAN = "SCAN", "Periodic scan"
        MANUAL = "MANUAL", "Manual backfill"

    default_priority = settings.LABELING_JOB_PRIORITY
    urgent_priority = settings.LABELING_JOB_PRIORITY  # labeling is never urgent

    # Scan jobs have no human owner; override the non-nullable base FK to allow null.
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="%(app_label)s_jobs",
    )
    queued_job_id: int | None
    queued_job = models.OneToOneField(
        ProcrastinateJob, null=True, on_delete=models.SET_NULL, related_name="+"
    )
    trigger = models.CharField(max_length=10, choices=Trigger.choices, default=Trigger.MANUAL)
    scan_from = models.DateTimeField(null=True, blank=True)
    # scan_from: set for SCAN jobs to the checkpoint timestamp at creation (window: created_at >= scan_from);
    # None for MANUAL jobs (full-corpus needs-work scope).

    tasks: models.QuerySet["LabelingTask"]

    # At most one LabelingJob may be active at any time (enforced by a partial unique index).
    ACTIVE_STATUSES = (
        AnalysisJob.Status.UNVERIFIED,
        AnalysisJob.Status.PREPARING,
        AnalysisJob.Status.PENDING,
        AnalysisJob.Status.IN_PROGRESS,
        AnalysisJob.Status.CANCELING,
    )

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"LabelingJob [{self.pk}]"

    def get_absolute_url(self) -> str:
        return reverse("admin:labels_labelingjob_change", args=[self.pk])

    def delay(self) -> None:
        queued_job_id = app.configure_task(
            "radis.labels.tasks.process_labeling_job",
            allow_unknown=False,
            priority=self.default_priority,
        ).defer(job_id=self.pk)
        self.queued_job_id = queued_job_id
        self.save()


class LabelingTask(AnalysisTask):
    job = models.ForeignKey(LabelingJob, on_delete=models.CASCADE, related_name="tasks")
    reports = models.ManyToManyField(Report, related_name="+")

    def get_absolute_url(self) -> str:
        return reverse("admin:labels_labelingtask_change", args=[self.pk])

    def delay(self) -> None:
        queued_job_id = app.configure_task(
            "radis.labels.tasks.process_labeling_task",
            allow_unknown=False,
            priority=self.job.default_priority,
        ).defer(task_id=self.pk)
        self.queued_job_id = queued_job_id
        self.save()
```

> Note: `AnalysisTask` already declares `queued_job`/`queued_job_id` (check `radis/core/models.py` — it does, lines ~231-234). So `LabelingTask` does NOT need to re-declare it; only `LabelingJob` does (the abstract `AnalysisJob` lacks it). Verify against `core/models.py` before adding; if `AnalysisTask` already has `queued_job`, do not duplicate it on `LabelingTask`.

- [ ] **Step 2: Generate the migration**, then add the partial unique index via `RunSQL`.

Run: `uv run python manage.py makemigrations labels` → creates `0002_*.py` with `LabelingJob`, `LabelingTask`. Then **edit that migration** to append a `RunSQL` operation (Django's `UniqueConstraint` can't express "unique over a constant expression," so use raw SQL):

```python
    operations = [
        # ... generated CreateModel operations ...
        migrations.RunSQL(
            sql=(
                "CREATE UNIQUE INDEX one_active_labeling_job "
                "ON labels_labelingjob ((true)) "
                "WHERE status IN ('UV', 'PR', 'PE', 'IP', 'CI');"
            ),
            reverse_sql="DROP INDEX IF EXISTS one_active_labeling_job;",
        ),
    ]
```

(The status codes are the DB values of `ACTIVE_STATUSES`: UNVERIFIED=`UV`, PREPARING=`PR`, PENDING=`PE`, IN_PROGRESS=`IP`, CANCELING=`CI` — confirm against `AnalysisJob.Status` in `radis/core/models.py`.)

- [ ] **Step 3: Add factories** to `radis/labels/factories.py`:

```python
from adit_radis_shared.accounts.factories import UserFactory

from .models import LabelingJob, LabelingTask


class LabelingJobFactory(BaseDjangoModelFactory[LabelingJob]):
    class Meta:
        model = LabelingJob

    owner = factory.SubFactory(UserFactory)
    trigger = LabelingJob.Trigger.MANUAL


class LabelingTaskFactory(BaseDjangoModelFactory[LabelingTask]):
    class Meta:
        model = LabelingTask

    job = factory.SubFactory(LabelingJobFactory)
```

- [ ] **Step 4: Write tests** `radis/labels/tests/test_jobs.py`:

```python
import pytest
from django.db import IntegrityError, transaction

from radis.labels.models import LabelingJob


@pytest.mark.django_db
def test_only_one_active_labeling_job_allowed():
    from radis.labels.factories import LabelingJobFactory

    LabelingJobFactory.create(status=LabelingJob.Status.PENDING)
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            LabelingJobFactory.create(status=LabelingJob.Status.IN_PROGRESS)


@pytest.mark.django_db
def test_finished_jobs_do_not_count_as_active():
    from radis.labels.factories import LabelingJobFactory

    LabelingJobFactory.create(status=LabelingJob.Status.SUCCESS)
    LabelingJobFactory.create(status=LabelingJob.Status.FAILURE)
    # A new active job is allowed because finished jobs are excluded from the partial index.
    LabelingJobFactory.create(status=LabelingJob.Status.PENDING)
    assert LabelingJob.objects.filter(status=LabelingJob.Status.PENDING).count() == 1


@pytest.mark.django_db
def test_scan_job_owner_may_be_null():
    job = LabelingJob.objects.create(
        trigger=LabelingJob.Trigger.SCAN, status=LabelingJob.Status.PENDING, owner=None
    )
    assert job.owner is None
```

- [ ] **Step 5: Run + verify** the migration applies and tests pass:
Run: `DJANGO_SETTINGS_MODULE=radis.settings.development uv run pytest radis/labels/tests/test_jobs.py -p no:cacheprovider -v`
Expected: 3 pass. If the partial-index test fails because factory `create` swallows the IntegrityError oddly, ensure the second create is inside `transaction.atomic()` (it is).

- [ ] **Step 6: Lint, type-check, commit**
Run `uv run ruff check radis/labels/`, `uv run pyright radis/labels/models.py radis/labels/tests/test_jobs.py`. Fix any findings (annotate `queued_job_id: int | None`, etc.). Commit: `feat(labels): add LabelingJob/LabelingTask with active-job singleton index`.

---

## Task 2: Backfill scope query `_needs_work_queryset`

The full-corpus predicate from the spec. This is the most intricate query in the feature.

**Files:**
- Create: `radis/labels/scope.py`
- Test: `radis/labels/tests/test_scope.py`

- [ ] **Step 1: Write the failing tests** `radis/labels/tests/test_scope.py`. Each builds a report in a specific state and asserts inclusion/exclusion. (`active_group_count` = number of `LabelGroup`s having ≥1 active label.)

```python
import pytest
from django.db.models import Count, Q

from radis.labels.factories import (
    GateAnswerFactory,
    LabelFactory,
    LabelGroupFactory,
    LabelResultFactory,
)
from radis.labels.models import GateAnswer, Label, LabelGroup, LabelResult
from radis.reports.factories import ReportFactory


def _active_group_count():
    return LabelGroup.objects.filter(labels__active=True).distinct().count()


@pytest.mark.django_db
def test_report_with_no_gate_answers_needs_work():
    from radis.labels.scope import _needs_work_queryset

    group = LabelGroupFactory.create()
    LabelFactory.create(group=group)
    report = ReportFactory.create()

    ids = list(_needs_work_queryset(_active_group_count()).values_list("pk", flat=True))
    assert report.pk in ids


@pytest.mark.django_db
def test_report_with_fresh_no_gate_and_no_results_is_done():
    from radis.labels.scope import _needs_work_queryset

    group = LabelGroupFactory.create()
    LabelFactory.create(group=group)
    report = ReportFactory.create()
    GateAnswerFactory.create(report=report, label_group=group, value=GateAnswer.Value.NO)

    ids = list(_needs_work_queryset(_active_group_count()).values_list("pk", flat=True))
    assert report.pk not in ids


@pytest.mark.django_db
def test_report_with_fresh_yes_gate_but_missing_result_needs_work():
    from radis.labels.scope import _needs_work_queryset

    group = LabelGroupFactory.create()
    LabelFactory.create(group=group)  # active label with no result
    report = ReportFactory.create()
    GateAnswerFactory.create(report=report, label_group=group, value=GateAnswer.Value.YES)

    ids = list(_needs_work_queryset(_active_group_count()).values_list("pk", flat=True))
    assert report.pk in ids


@pytest.mark.django_db
def test_report_with_fresh_yes_gate_and_fresh_results_is_done():
    from radis.labels.scope import _needs_work_queryset

    group = LabelGroupFactory.create()
    label = LabelFactory.create(group=group)
    report = ReportFactory.create()
    GateAnswerFactory.create(report=report, label_group=group, value=GateAnswer.Value.YES)
    LabelResultFactory.create(report=report, label=label, value=LabelResult.Value.PRESENT)

    ids = list(_needs_work_queryset(_active_group_count()).values_list("pk", flat=True))
    assert report.pk not in ids


@pytest.mark.django_db
def test_report_with_fresh_yes_gate_and_stale_result_needs_work():
    from radis.labels.scope import _needs_work_queryset

    group = LabelGroupFactory.create()
    label = LabelFactory.create(group=group)
    report = ReportFactory.create()
    GateAnswerFactory.create(report=report, label_group=group, value=GateAnswer.Value.YES)
    LabelResultFactory.create(report=report, label=label, value=LabelResult.Value.PRESENT)
    label.description = "edited"  # bump label.updated_at -> result becomes stale
    label.save()

    ids = list(_needs_work_queryset(_active_group_count()).values_list("pk", flat=True))
    assert report.pk in ids


@pytest.mark.django_db
def test_report_with_only_absent_but_fresh_results_is_done():
    from radis.labels.scope import _needs_work_queryset

    group = LabelGroupFactory.create()
    label = LabelFactory.create(group=group)
    report = ReportFactory.create()
    GateAnswerFactory.create(report=report, label_group=group, value=GateAnswer.Value.YES)
    LabelResultFactory.create(report=report, label=label, value=LabelResult.Value.ABSENT)

    ids = list(_needs_work_queryset(_active_group_count()).values_list("pk", flat=True))
    assert report.pk not in ids


@pytest.mark.django_db
def test_report_with_stale_gate_needs_work():
    from radis.labels.scope import _needs_work_queryset

    group = LabelGroupFactory.create()
    LabelFactory.create(group=group)
    report = ReportFactory.create()
    GateAnswerFactory.create(report=report, label_group=group, value=GateAnswer.Value.NO)
    group.gate_question = "changed?"  # bump group.updated_at -> gate stale
    group.save()

    ids = list(_needs_work_queryset(_active_group_count()).values_list("pk", flat=True))
    assert report.pk in ids
```

- [ ] **Step 2: Run to verify failure** (`ModuleNotFoundError: radis.labels.scope`).

- [ ] **Step 3: Implement** `radis/labels/scope.py` (verbatim from the spec's "Needs work predicate"):

```python
from django.db.models import Count, Exists, F, OuterRef, Q, QuerySet

from .models import GateAnswer, Label
from radis.reports.models import Report


def _needs_work_queryset(active_group_count: int) -> QuerySet:
    """Reports needing labeling work: missing/stale gate (A) OR a YES/MAYBE group with a
    missing/stale label result (B)."""
    return Report.objects.annotate(
        non_stale_gate_count=Count(
            "gate_answers",
            filter=Q(
                gate_answers__label_group__labels__active=True,
                gate_answers__generated_at__gte=F("gate_answers__label_group__updated_at"),
            ),
            distinct=True,
        ),
    ).filter(
        Q(non_stale_gate_count__lt=active_group_count)
        | Exists(
            GateAnswer.objects.filter(
                report=OuterRef("pk"),
                value__in=[GateAnswer.Value.YES, GateAnswer.Value.MAYBE],
                generated_at__gte=F("label_group__updated_at"),
            ).filter(
                Exists(
                    Label.objects.filter(
                        group_id=OuterRef("label_group_id"),
                        active=True,
                    ).exclude(
                        results__report_id=OuterRef(OuterRef("pk")),
                        results__generated_at__gte=F("updated_at"),
                    )
                )
            )
        )
    )
```

- [ ] **Step 4: Run to verify pass** (7 tests). If condition A's `non_stale_gate_count` over-counts due to the `labels__active` join multiplying rows, the `distinct=True` on the `Count` handles it — confirm the "fresh NO gate, done" test passes (it exercises A precisely).

- [ ] **Step 5: Lint, type-check, commit**: `feat(labels): add backfill needs-work scope query`.

---

## Task 3: `process_labeling_job` (PREPARING phase) + task streaming

**Files:**
- Create: `radis/labels/tasks.py` (job processing portion; the periodic task is added in Task 5)
- Test: `radis/labels/tests/test_jobs.py` (append)

- [ ] **Step 1: Write failing tests** (append to `test_jobs.py`). Mock the LLM so no real calls happen; here we only assert task *creation*, not label results.

```python
@pytest.mark.django_db
def test_manual_job_creates_tasks_for_needs_work_reports(monkeypatch):
    from radis.labels import tasks
    from radis.labels.factories import LabelFactory, LabelGroupFactory, LabelingJobFactory
    from radis.labels.models import LabelingJob, LabelingTask
    from radis.reports.factories import ReportFactory

    group = LabelGroupFactory.create()
    LabelFactory.create(group=group)
    ReportFactory.create()
    ReportFactory.create()
    job = LabelingJobFactory.create(trigger=LabelingJob.Trigger.MANUAL,
                                    status=LabelingJob.Status.PENDING)

    # Prevent real task enqueue.
    monkeypatch.setattr(LabelingTask, "delay", lambda self: None)
    tasks.process_labeling_job(job.pk)

    job.refresh_from_db()
    assert job.tasks.exists()
    report_ids = set()
    for task in job.tasks.all():
        report_ids.update(task.reports.values_list("pk", flat=True))
    assert len(report_ids) == 2


@pytest.mark.django_db
def test_scan_job_only_includes_reports_after_scan_from(monkeypatch):
    from datetime import timedelta
    from django.utils import timezone
    from radis.labels import tasks
    from radis.labels.factories import LabelFactory, LabelGroupFactory, LabelingJobFactory
    from radis.labels.models import LabelingJob, LabelingTask
    from radis.reports.factories import ReportFactory

    group = LabelGroupFactory.create()
    LabelFactory.create(group=group)
    old = ReportFactory.create()
    Report = old.__class__
    Report.objects.filter(pk=old.pk).update(created_at=timezone.now() - timedelta(days=10))
    cutoff = timezone.now() - timedelta(days=1)
    new = ReportFactory.create()  # created now (after cutoff)

    job = LabelingJobFactory.create(trigger=LabelingJob.Trigger.SCAN,
                                    scan_from=cutoff, status=LabelingJob.Status.PENDING)
    monkeypatch.setattr(LabelingTask, "delay", lambda self: None)
    tasks.process_labeling_job(job.pk)

    job.refresh_from_db()
    included = set()
    for task in job.tasks.all():
        included.update(task.reports.values_list("pk", flat=True))
    assert new.pk in included
    assert old.pk not in included


@pytest.mark.django_db
def test_process_job_is_idempotent_on_retry(monkeypatch):
    """Re-running process_labeling_job wipes partial tasks from a prior attempt (no duplicates)."""
    from radis.labels import tasks
    from radis.labels.factories import LabelFactory, LabelGroupFactory, LabelingJobFactory
    from radis.labels.models import LabelingJob, LabelingTask

    from radis.reports.factories import ReportFactory

    LabelFactory.create(group=LabelGroupFactory.create())
    ReportFactory.create()
    job = LabelingJobFactory.create(trigger=LabelingJob.Trigger.MANUAL,
                                    status=LabelingJob.Status.PENDING)
    monkeypatch.setattr(LabelingTask, "delay", lambda self: None)

    tasks.process_labeling_job(job.pk)
    first_count = job.tasks.count()
    tasks.process_labeling_job(job.pk)  # simulate Procrastinate retry
    assert job.tasks.count() == first_count
```

- [ ] **Step 2: Run to verify failure.**

- [ ] **Step 3: Implement** `radis/labels/tasks.py` (job portion). Mirror `radis/extractions/tasks.py`'s PREPARING→PENDING invariant.

```python
import logging

from django.conf import settings
from django.utils import timezone
from procrastinate.contrib.django import app

from radis.core.models import AnalysisJob, AnalysisTask
from radis.reports.models import Report

from .models import LabelingJob, LabelingTask
from .scope import _needs_work_queryset

logger = logging.getLogger(__name__)


def _scope_queryset(job: LabelingJob):
    if job.scan_from is not None:  # SCAN job: recent window
        return Report.objects.filter(created_at__gte=job.scan_from).order_by("pk")
    active_group_count = (
        LabelGroup.objects.filter(labels__active=True).distinct().count()
    )
    return _needs_work_queryset(active_group_count).order_by("pk")


def _create_labeling_tasks_streaming(job: LabelingJob) -> None:
    batch: list[int] = []
    for report_id in (
        _scope_queryset(job).values_list("pk", flat=True).iterator(chunk_size=settings.LABELING_TASK_BATCH_SIZE)
    ):
        batch.append(report_id)
        if len(batch) >= settings.LABELING_TASK_BATCH_SIZE:
            _flush_task(job, batch)
            batch = []
    if batch:
        _flush_task(job, batch)


def _flush_task(job: LabelingJob, report_ids: list[int]) -> None:
    task = LabelingTask.objects.create(job=job, status=AnalysisTask.Status.PENDING)
    task.reports.add(*report_ids)


@app.task()
def process_labeling_job(job_id: int) -> None:
    job = LabelingJob.objects.get(id=job_id)
    job.tasks.all().delete()  # wipe partial rows from any prior crashed attempt (idempotent)
    job.status = AnalysisJob.Status.PREPARING
    job.started_at = timezone.now()
    job.save()

    _create_labeling_tasks_streaming(job)

    job.status = AnalysisJob.Status.PENDING
    job.queued_job_id = None
    job.save()

    # Only now (PENDING) may tasks be enqueued.
    for task in job.tasks.filter(status=AnalysisTask.Status.PENDING):
        if not task.is_queued:
            task.delay()
```

(Add `from .models import LabelGroup` to the imports — it's used in `_scope_queryset`.)

- [ ] **Step 4: Run to verify pass** (3 new tests). The `iterator(chunk_size=...)` server-side cursor freezing behavior is described in the spec; tests don't exercise concurrency but confirm scope correctness and idempotency.

- [ ] **Step 5: Lint, type-check, commit**: `feat(labels): add labeling job preparation and task streaming`.

---

## Task 4: `LabelingTaskProcessor` + `process_labeling_task`

**Files:**
- Create: `radis/labels/processors.py`
- Modify: `radis/labels/tasks.py` (add `process_labeling_task`)
- Test: `radis/labels/tests/test_processor.py`

- [ ] **Step 1: Write failing tests** `radis/labels/tests/test_processor.py`. Patch `label_report` to control success/failure per report and assert task status.

```python
import pytest

from radis.core.models import AnalysisTask
from radis.labels.factories import LabelingJobFactory, LabelingTaskFactory
from radis.labels.models import LabelingJob
from radis.reports.factories import ReportFactory


@pytest.mark.django_db
def test_processor_calls_label_report_for_each_report(monkeypatch):
    from radis.labels import processors

    called = []
    monkeypatch.setattr(processors, "label_report", lambda rid: called.append(rid))

    job = LabelingJobFactory.create(status=LabelingJob.Status.PENDING)
    task = LabelingTaskFactory.create(job=job, status=AnalysisTask.Status.PENDING)
    r1, r2 = ReportFactory.create(), ReportFactory.create()
    task.reports.add(r1, r2)

    processors.LabelingTaskProcessor(task).start()

    task.refresh_from_db()
    assert set(called) == {r1.pk, r2.pk}
    assert task.status == AnalysisTask.Status.SUCCESS


@pytest.mark.django_db
def test_processor_one_report_failure_yields_warning(monkeypatch):
    from radis.labels import processors

    r_ok, r_bad = ReportFactory.create(), ReportFactory.create()

    def fake_label_report(rid):
        if rid == r_bad.pk:
            raise RuntimeError("LLM exploded")

    monkeypatch.setattr(processors, "label_report", fake_label_report)

    job = LabelingJobFactory.create(status=LabelingJob.Status.PENDING)
    task = LabelingTaskFactory.create(job=job, status=AnalysisTask.Status.PENDING)
    task.reports.add(r_ok, r_bad)

    processors.LabelingTaskProcessor(task).start()

    task.refresh_from_db()
    assert task.status == AnalysisTask.Status.WARNING  # partial failure -> WARNING, not FAILURE
```

- [ ] **Step 2: Run to verify failure.**

- [ ] **Step 3: Implement** `radis/labels/processors.py`. Mirror `radis/extractions/processors.py` (thread pool) but call `label_report`. Per the spec: a single report's failure must NOT abort the batch; partial failure → task `WARNING`.

```python
import logging
from concurrent.futures import Future, ThreadPoolExecutor

from django import db
from django.conf import settings

from radis.core.models import AnalysisTask
from radis.core.processors import AnalysisTaskProcessor

from .labeling import label_report
from .models import LabelingTask

logger = logging.getLogger(__name__)


class LabelingTaskProcessor(AnalysisTaskProcessor):
    def process_task(self, task: LabelingTask) -> None:
        had_failure = False
        with ThreadPoolExecutor(max_workers=settings.LABELING_LLM_CONCURRENCY_LIMIT) as executor:
            try:
                futures: dict[Future, int] = {}
                for report_id in task.reports.values_list("pk", flat=True):
                    futures[executor.submit(self._safe_label, report_id)] = report_id
                for future in futures:
                    if future.result() is False:
                        had_failure = True
            finally:
                db.close_old_connections()

        if had_failure:
            task.status = AnalysisTask.Status.WARNING
            task.message = "Some reports failed to label; see logs."

    def _safe_label(self, report_id: int) -> bool:
        try:
            label_report(report_id)
            return True
        except Exception:
            logger.exception("Labeling failed for report %s", report_id)
            return False
        finally:
            db.close_old_connections()
```

> The base `AnalysisTaskProcessor.start()` (in `radis/core/processors.py`) sets the task to SUCCESS only if `process_task` left it IN_PROGRESS. Setting `WARNING` here overrides that, matching the spec. Confirm this against `core/processors.py:59` ("if task.status == IN_PROGRESS: SUCCESS").

- [ ] **Step 4: Add `process_labeling_task`** to `radis/labels/tasks.py`:

```python
from .processors import LabelingTaskProcessor


@app.task(queue="llm")
def process_labeling_task(task_id: int) -> None:
    task = LabelingTask.objects.get(id=task_id)
    LabelingTaskProcessor(task).start()
    task = LabelingTask.objects.get(id=task_id)
    task.queued_job_id = None
    task.save()
```

- [ ] **Step 5: Run to verify pass** (2 tests). Note: `AnalysisTaskProcessor.start()` asserts the job is PENDING/IN_PROGRESS; the factory sets job PENDING so the assert passes.

- [ ] **Step 6: Lint, type-check, commit**: `feat(labels): add labeling task processor with per-report isolation`.

---

## Task 5: Periodic incremental scan + checkpoint

**Files:**
- Modify: `radis/labels/tasks.py` (add `incremental_label_scan`)
- Modify: `radis/labels/apps.py` (ensure tasks module is imported so the periodic task registers)
- Test: `radis/labels/tests/test_scan.py`

- [ ] **Step 1: Write failing tests** `radis/labels/tests/test_scan.py`. Call the task body directly with a timestamp; patch `LabelingJob.delay` to avoid real enqueue.

```python
import time

import pytest
from django.utils import timezone

from radis.labels.factories import LabelFactory, LabelGroupFactory, LabelingJobFactory
from radis.labels.models import LabelingJob, LabelingScanCheckpoint
from radis.reports.factories import ReportFactory


def _now_ts():
    return int(time.time())


@pytest.mark.django_db
def test_first_run_sets_checkpoint_and_creates_no_job(monkeypatch):
    from radis.labels import tasks

    monkeypatch.setattr(LabelingJob, "delay", lambda self: None)
    tasks.incremental_label_scan(_now_ts())

    cp = LabelingScanCheckpoint.objects.get(pk=1)
    assert cp.last_scanned_at is not None
    assert not LabelingJob.objects.exists()


@pytest.mark.django_db
def test_active_job_guard_skips_without_advancing(monkeypatch):
    from radis.labels import tasks

    LabelingScanCheckpoint.objects.create(last_scanned_at=timezone.now())
    before = LabelingScanCheckpoint.objects.get(pk=1).last_scanned_at
    LabelingJobFactory.create(status=LabelingJob.Status.IN_PROGRESS)

    monkeypatch.setattr(LabelingJob, "delay", lambda self: None)
    tasks.incremental_label_scan(_now_ts())

    after = LabelingScanCheckpoint.objects.get(pk=1).last_scanned_at
    assert after == before  # unchanged
    assert LabelingJob.objects.filter(trigger=LabelingJob.Trigger.SCAN).count() == 0


@pytest.mark.django_db
def test_no_new_reports_advances_checkpoint_without_job(monkeypatch):
    from radis.labels import tasks

    LabelFactory.create(group=LabelGroupFactory.create())
    LabelingScanCheckpoint.objects.create(last_scanned_at=timezone.now())
    before = LabelingScanCheckpoint.objects.get(pk=1).last_scanned_at

    monkeypatch.setattr(LabelingJob, "delay", lambda self: None)
    tasks.incremental_label_scan(_now_ts())

    after = LabelingScanCheckpoint.objects.get(pk=1).last_scanned_at
    assert after > before
    assert not LabelingJob.objects.exists()


@pytest.mark.django_db
def test_new_reports_create_scan_job_and_advance(monkeypatch):
    from radis.labels import tasks

    LabelFactory.create(group=LabelGroupFactory.create())
    from datetime import timedelta
    LabelingScanCheckpoint.objects.create(last_scanned_at=timezone.now() - timedelta(hours=1))
    ReportFactory.create()  # created now, after checkpoint

    delayed = []
    monkeypatch.setattr(LabelingJob, "delay", lambda self: delayed.append(self.pk))
    tasks.incremental_label_scan(_now_ts())

    job = LabelingJob.objects.get(trigger=LabelingJob.Trigger.SCAN)
    assert job.scan_from is not None
    assert delayed == [job.pk]
```

- [ ] **Step 2: Run to verify failure.**

- [ ] **Step 3: Implement** `incremental_label_scan` in `radis/labels/tasks.py` (verbatim logic from the spec's "Create scan job"):

```python
from datetime import datetime, timezone as dt_timezone

from .models import LabelingScanCheckpoint, Label


@app.periodic(cron=settings.LABELING_SCAN_CRON)
@app.task()
def incremental_label_scan(timestamp: int) -> None:
    now = datetime.fromtimestamp(timestamp, tz=dt_timezone.utc)
    checkpoint, _ = LabelingScanCheckpoint.objects.get_or_create(pk=1)

    if LabelingJob.objects.filter(status__in=LabelingJob.ACTIVE_STATUSES).exists():
        logger.info("Active LabelingJob found, skipping scan tick (checkpoint unchanged).")
        return

    if checkpoint.last_scanned_at is None:
        checkpoint.last_scanned_at = now  # first run: existing reports belong to a manual backfill
        checkpoint.save()
        return

    if not Label.objects.filter(active=True).exists():
        return

    if Report.objects.filter(created_at__gte=checkpoint.last_scanned_at).exists():
        job = LabelingJob.objects.create(
            trigger=LabelingJob.Trigger.SCAN, scan_from=checkpoint.last_scanned_at,
            status=AnalysisJob.Status.PENDING,
        )
        job.delay()

    checkpoint.last_scanned_at = now
    checkpoint.save()
```

> Decision: scan jobs are created already-`PENDING` (no human verification step), owner left null. If `START_..._UNVERIFIED`-style gating is desired later, revisit. The `delay()` defers `process_labeling_job`, which sets PREPARING then PENDING.

- [ ] **Step 4: Ensure registration.** In `radis/labels/apps.py`, add a `ready()` that imports tasks so `@app.periodic` registers (mirror how other apps do it — check `radis/subscriptions/apps.py`). If Procrastinate auto-discovers `tasks.py` via the Django integration (as it may already for extractions/subscriptions), confirm by running the app; only add the import if needed.

- [ ] **Step 5: Run to verify pass** (4 tests).

- [ ] **Step 6: Lint, type-check, commit**: `feat(labels): add periodic incremental scan and checkpoint advancement`.

---

## Task 6: Admin (authoring + backfill cockpit + read-only ops views)

**Files:**
- Create: `radis/labels/admin.py`
- Modify: `radis/reports/admin.py` (add `LabelResultInline`)
- Test: `radis/labels/tests/test_admin.py`

- [ ] **Step 1: Implement `radis/labels/admin.py`.** Build the admin classes from the spec's "Admin UX" section: `LabelGroupAdmin`, `LabelAdmin` (autocomplete group FK), `LabelResultAdmin` (read-only, `is_stale` annotation), `GateAnswerAdmin` (read-only), `LabelingScanCheckpointAdmin` (read-only singleton), and `LabelingJobAdmin`. For the **backfill cockpit**, the simplest robust mechanism (vs. a custom template banner) is an admin action / a `response_change` "Run backfill" button that creates a `MANUAL` `LabelingJob` owned by `request.user` and calls `delay()`, guarded by the singleton (catch `IntegrityError` → message "A labeling job is already active"). Follow Django admin conventions; reference any existing custom-admin in the repo. Concretely:

```python
from django.contrib import admin, messages
from django.db import IntegrityError
from django.db.models import Count, F, Q

from .models import (
    GateAnswer, Label, LabelGroup, LabelResult, LabelingJob, LabelingScanCheckpoint, LabelingTask,
)


@admin.register(LabelGroup)
class LabelGroupAdmin(admin.ModelAdmin):
    list_display = ("name", "gate_question", "updated_at")
    search_fields = ("name",)  # required for LabelAdmin autocomplete
    ordering = ("name",)
    readonly_fields = ("updated_at",)


@admin.register(Label)
class LabelAdmin(admin.ModelAdmin):
    autocomplete_fields = ["group"]
    list_display = ("name", "group", "active", "updated_at")
    list_filter = ("active", "group")
    search_fields = ("name", "group__name", "description")
    ordering = ("group__name", "name")
    readonly_fields = ("created_at", "updated_at")


class _ReadOnlyAdmin(admin.ModelAdmin):
    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(LabelResult)
class LabelResultAdmin(_ReadOnlyAdmin):
    list_display = ("report", "label", "value", "is_stale", "generated_at")
    list_filter = ("value", "label")
    search_fields = ("report__document_id", "label__name")
    raw_id_fields = ("report", "label")

    def get_queryset(self, request):
        return super().get_queryset(request).annotate(
            _stale=Q(generated_at__lt=F("label__updated_at"))
        )

    @admin.display(boolean=True, description="Stale")
    def is_stale(self, obj) -> bool:
        return obj.generated_at < obj.label.updated_at


@admin.register(GateAnswer)
class GateAnswerAdmin(_ReadOnlyAdmin):
    list_display = ("report", "label_group", "value", "is_stale", "generated_at")
    list_filter = ("value", "label_group")
    search_fields = ("report__document_id", "label_group__name")
    raw_id_fields = ("report", "label_group")

    @admin.display(boolean=True, description="Stale")
    def is_stale(self, obj) -> bool:
        return obj.generated_at < obj.label_group.updated_at


@admin.register(LabelingScanCheckpoint)
class LabelingScanCheckpointAdmin(admin.ModelAdmin):
    list_display = ("last_scanned_at",)
    readonly_fields = ("last_scanned_at",)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(LabelingJob)
class LabelingJobAdmin(admin.ModelAdmin):
    list_display = ("id", "trigger", "status", "owner", "created_at", "ended_at")
    list_filter = ("trigger", "status")
    readonly_fields = ("trigger", "scan_from", "status", "created_at", "started_at", "ended_at")
    actions = ["run_backfill"]

    @admin.action(description="Run a manual backfill now")
    def run_backfill(self, request, queryset):
        try:
            job = LabelingJob.objects.create(
                trigger=LabelingJob.Trigger.MANUAL,
                status=LabelingJob.Status.PENDING,
                owner=request.user,
            )
        except IntegrityError:
            self.message_user(request, "A labeling job is already active.", level=messages.ERROR)
            return
        job.delay()
        self.message_user(request, f"Started backfill job {job.pk}.", level=messages.SUCCESS)


@admin.register(LabelingTask)
class LabelingTaskAdmin(_ReadOnlyAdmin):
    list_display = ("id", "job", "status", "started_at", "ended_at")
    list_filter = ("status",)
    raw_id_fields = ("job",)
```

> The `run_backfill` action ignores `queryset` (it creates a new job) — Django actions require a queryset selection, which is awkward for a "create" action. If a cleaner "Run" affordance is desired, add a custom `change_list_template` with a button POSTing to a custom admin URL; but the action form is the minimal mechanism that satisfies the spec's "Run" + singleton-conflict messaging. Pick the action form for v1.

- [ ] **Step 2: Add the Report inline.** In `radis/reports/admin.py`, add:

```python
from radis.labels.models import LabelResult


class LabelResultInline(admin.TabularInline):
    model = LabelResult
    extra = 0
    can_delete = False
    readonly_fields = ("label", "value", "generated_at")
    raw_id_fields = ("label",)

    def has_add_permission(self, request, obj=None):
        return False
```

…and append `LabelResultInline` to `ReportAdmin.inlines`. (Read `radis/reports/admin.py` first to match its existing structure.)

- [ ] **Step 3: Write admin tests** `radis/labels/tests/test_admin.py` using Django's admin client (staff superuser):

```python
import pytest
from adit_radis_shared.accounts.factories import UserFactory

from radis.labels.models import LabelingJob


@pytest.mark.django_db
def test_run_backfill_action_creates_manual_job(client):
    admin_user = UserFactory.create(is_staff=True, is_superuser=True, is_active=True)
    client.force_login(admin_user)
    # Trigger the admin action via POST to the changelist.
    resp = client.post(
        "/admin/labels/labelingjob/",
        {"action": "run_backfill", "_selected_action": []},
        follow=True,
    )
    assert LabelingJob.objects.filter(trigger=LabelingJob.Trigger.MANUAL).count() == 1


@pytest.mark.django_db
def test_run_backfill_conflict_when_active_job_exists(client, monkeypatch):
    from radis.labels.factories import LabelingJobFactory

    monkeypatch.setattr(LabelingJob, "delay", lambda self: None)
    LabelingJobFactory.create(status=LabelingJob.Status.IN_PROGRESS)
    admin_user = UserFactory.create(is_staff=True, is_superuser=True, is_active=True)
    client.force_login(admin_user)
    client.post("/admin/labels/labelingjob/",
                {"action": "run_backfill", "_selected_action": []}, follow=True)
    # No second active job created.
    assert LabelingJob.objects.filter(status__in=LabelingJob.ACTIVE_STATUSES).count() == 1
```

> Note: Django admin actions normally require selected rows; posting an empty `_selected_action` may short-circuit. If the test reveals the action doesn't fire without a selection, switch the cockpit to a custom changelist button + URL (see Step 1 note) and update the test to POST that URL. Verify the actual behavior and adjust — do not leave the test asserting nothing.

- [ ] **Step 4: Run + verify** `DJANGO_SETTINGS_MODULE=radis.settings.development uv run pytest radis/labels/tests/test_admin.py -p no:cacheprovider -v`. Also `monkeypatch` `LabelingJob.delay` in the create test if `delay()` would try to defer to a real Procrastinate (it will — patch it).

- [ ] **Step 5: Lint, type-check, commit**: `feat(labels): add admin authoring, ops views, and backfill cockpit`.

---

## Task 7: Full-suite verification + settings/registration audit

**Files:** none (verification)

- [ ] **Step 1: Confirm periodic registration.** Run a Django check / start to confirm `incremental_label_scan` registers without error: `DJANGO_SETTINGS_MODULE=radis.settings.development uv run python manage.py check`.
- [ ] **Step 2: Full suite.** `DJANGO_SETTINGS_MODULE=radis.settings.development uv run pytest radis/labels/ -p no:cacheprovider -q` — all Plan 1 + Plan 2 tests pass.
- [ ] **Step 3: Lint + format + pyright.** `uv run ruff check radis/labels/ radis/reports/admin.py`; `uv run ruff format --check radis/labels/`; `uv run pyright radis/labels/`. Fix inline.
- [ ] **Step 4: Commit** any fixups: `chore(labels): lint/type-check Plan 2`.

---

## Plan 2 Definition of Done

- `LabelingJob`/`LabelingTask` exist; a DB partial unique index enforces one active job; migration applies.
- `process_labeling_job` prepares batched tasks from the correct scope (SCAN window vs. MANUAL needs-work) and is retry-idempotent; tasks enqueue only after PENDING.
- `LabelingTaskProcessor` runs `label_report` per report with per-report failure isolation (partial failure → task WARNING).
- `incremental_label_scan` advances the singleton checkpoint and creates SCAN jobs per the guard/first-run/no-new/has-new cases.
- Admin: authoring (`LabelGroupAdmin`, `LabelAdmin`), read-only ops views, the Report inline, and a working "Run backfill" cockpit with singleton-conflict messaging.
- Full suite + ruff + pyright clean.

**Next:** `writing-plans` for **Plan 3 — Surfacing + Observability**.
