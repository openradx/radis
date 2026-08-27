# Worker-Crash Task Recovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Tasks left `IN_PROGRESS` by a killed worker are repaired automatically — by a startup sweep at container boot and a periodic sweep every minute — while the processor only ever executes tasks it atomically claimed from `PENDING`.

**Architecture:** One repair function (`sweep_stale_analysis_state`) invoked from two entry points: a management command run before `bg_worker` in each worker container, and a `@app.periodic` Procrastinate task. `AnalysisTaskProcessor.start()` replaces its status assert with an atomic conditional-UPDATE claim and never repairs. All concurrent writes go through conditional UPDATEs; the sweep re-checks liveness inside its UPDATE and decides re-queueing from a fresh post-UPDATE read.

**Tech Stack:** Django 6, PostgreSQL 17, Procrastinate (via `procrastinate.contrib.django`), pytest + pytest-django + factory-boy + time_machine.

**Spec:** `docs/superpowers/specs/2026-08-01-worker-crash-task-recovery.md` — read it first; its "Why every ordering converges" section is the rationale for the conditional-UPDATE patterns below. Do not simplify them into read-then-save.

## Global Constraints

- Line length 100 (ruff), Google style, terse comments/docstrings matching house style.
- `ANALYSIS_STALLED_WORKER_GRACE_SECONDS` default `30`; documented as never-below-30.
- `ANALYSIS_SWEEP_CRON` default `* * * * *` (every minute).
- Never write to Procrastinate's own rows (they are read-only Django models); creating new rows via `task.delay()` is allowed.
- The `sweep_stale_tasks` management command must **never** exit non-zero (it gates worker boot via `&&`).
- Test runs: `uv run pytest <path> -v`. Full suite: `uv run cli test`. Lint: `uv run cli lint`.
- Commit messages: conventional commits with scope (`feat(core): …`, `test(labels): …`), ending with the repo's standard Claude co-author line.
- Procrastinate Django models are read-only by default; tests that create `ProcrastinateJob`/`ProcrastinateWorker` rows must set `settings.PROCRASTINATE_READONLY_MODELS = False` (the pytest-django `settings` fixture works — the flag is read dynamically).

## Codebase facts discovered during planning (trust these, they were verified)

- `SubscribedItem` **already has** the `(subscription, report)` UniqueConstraint (`radis/subscriptions/models.py:113-120`) and `process_report` already has an early `exists()` skip (`radis/subscriptions/processors.py:72`). Spec change 7's remaining work for subscriptions is only `create()` → `get_or_create()`.
- `radis/labels/tasks.py:47-57` already has the prep re-entry guard; labels need nothing for spec change 8.
- Latest subscriptions migration is `0011_filter_questions_and_extraction_results` — the new on-delete migration is `0012` and depends on it.
- Concrete `AnalysisTask` subclasses: `LabelingTask`, `ExtractionTask`, `SubscriptionTask`. Their `delay()` implementations do a **full `self.save()`** after deferring — the sweep must `refresh_from_db()` before calling `delay()`, or the stale in-memory instance would write `IN_PROGRESS` back over the just-repaired `PENDING`.
- `docker-compose.base.yml` assigns `hostname: web.local` / `init.local`, so `wait-for-it -s web.local:8000` (dev) and `init.local:8000` (prod) resolve.
- `procrastinate.contrib.django.app.periodic_registry.periodic_tasks` is a `dict[tuple[str, str], PeriodicTask]`; `PeriodicTask` has `.task`, `.cron`; `Task` has `.name`, `.queueing_lock`.
- Procrastinate-task functions decorated with `@app.task` are directly callable in tests (see `radis/labels/tests/test_scan.py:21`).

---

### Task 1: DB-level `ON DELETE SET NULL` migrations (labels + subscriptions)

Procrastinate deletes queue rows via raw SQL, bypassing Django's `SET_NULL`; only `extractions` has the DB-level fix. The sweep's `queued_job_id IS NULL` orphan branch relies on this.

**Files:**
- Create: `radis/labels/migrations/0002_procrastinate_on_delete.py`
- Create: `radis/subscriptions/migrations/0012_procrastinate_on_delete.py`
- Test: `radis/labels/tests/test_models.py` (append), `radis/subscriptions/tests/test_constraints.py` (append)

**Interfaces:**
- Consumes: `adit_radis_shared.common.utils.migration_utils.procrastinate_on_delete_sql(app_label, model_name, reverse=False)` (same helper as `radis/extractions/migrations/0002_procrastinate_on_delete.py`).
- Produces: raw `DELETE FROM procrastinate_jobs` nulls `queued_job_id` on `LabelingJob`/`LabelingTask`/`SubscriptionJob`/`SubscriptionTask` rows. Task 4's tests depend on this behavior for `ExtractionTask` (already migrated) and Task 10's for `LabelingTask`.

- [ ] **Step 1: Write the failing test for labels**

Append to `radis/labels/tests/test_models.py`:

```python
@pytest.mark.django_db
def test_raw_queue_row_delete_nulls_labeling_task_fk(settings):
    settings.PROCRASTINATE_READONLY_MODELS = False
    from django.db import connection
    from procrastinate.contrib.django.models import ProcrastinateJob

    from radis.labels.factories import LabelingJobFactory, LabelingTaskFactory

    row = ProcrastinateJob.objects.create(
        queue_name="llm",
        task_name="radis.labels.tasks.process_labeling_task",
        priority=0,
        args={},
        status="todo",
        attempts=0,
        abort_requested=False,
    )
    task = LabelingTaskFactory.create(job=LabelingJobFactory.create(), queued_job=row)

    # Procrastinate deletes rows via raw SQL, bypassing Django's SET_NULL.
    with connection.cursor() as cursor:
        cursor.execute("DELETE FROM procrastinate_jobs WHERE id = %s", [row.pk])

    task.refresh_from_db()
    assert task.queued_job_id is None
```

Add `import pytest` at the top if not already present.

- [ ] **Step 2: Write the failing test for subscriptions**

Append to `radis/subscriptions/tests/test_constraints.py` (same body, different factories/task name):

```python
@pytest.mark.django_db
def test_raw_queue_row_delete_nulls_subscription_task_fk(settings):
    settings.PROCRASTINATE_READONLY_MODELS = False
    from django.db import connection
    from procrastinate.contrib.django.models import ProcrastinateJob

    from radis.subscriptions.factories import SubscriptionTaskFactory

    row = ProcrastinateJob.objects.create(
        queue_name="llm",
        task_name="radis.subscriptions.tasks.process_subscription_task",
        priority=0,
        args={},
        status="todo",
        attempts=0,
        abort_requested=False,
    )
    task = SubscriptionTaskFactory.create(queued_job=row)

    with connection.cursor() as cursor:
        cursor.execute("DELETE FROM procrastinate_jobs WHERE id = %s", [row.pk])

    task.refresh_from_db()
    assert task.queued_job_id is None
```

- [ ] **Step 3: Run both tests to verify they fail**

Run: `uv run pytest radis/labels/tests/test_models.py::test_raw_queue_row_delete_nulls_labeling_task_fk radis/subscriptions/tests/test_constraints.py::test_raw_queue_row_delete_nulls_subscription_task_fk -v`
Expected: FAIL — `queued_job_id` still set (the FK check is deferred inside the test transaction, so the delete leaves a dangling id).

- [ ] **Step 4: Write the labels migration**

Create `radis/labels/migrations/0002_procrastinate_on_delete.py` (mirror of the extractions one):

```python
from django.db import migrations

from adit_radis_shared.common.utils.migration_utils import procrastinate_on_delete_sql


class Migration(migrations.Migration):
    dependencies = [
        ("labels", "0001_initial"),
        ("procrastinate", "0028_add_cancel_states"),
    ]

    operations = [
        migrations.RunSQL(
            sql=procrastinate_on_delete_sql("labels", "labelingjob"),
            reverse_sql=procrastinate_on_delete_sql("labels", "labelingjob", reverse=True),
        ),
        migrations.RunSQL(
            sql=procrastinate_on_delete_sql("labels", "labelingtask"),
            reverse_sql=procrastinate_on_delete_sql("labels", "labelingtask", reverse=True),
        ),
    ]
```

- [ ] **Step 5: Write the subscriptions migration**

Create `radis/subscriptions/migrations/0012_procrastinate_on_delete.py`:

```python
from django.db import migrations

from adit_radis_shared.common.utils.migration_utils import procrastinate_on_delete_sql


class Migration(migrations.Migration):
    dependencies = [
        ("subscriptions", "0011_filter_questions_and_extraction_results"),
        ("procrastinate", "0028_add_cancel_states"),
    ]

    operations = [
        migrations.RunSQL(
            sql=procrastinate_on_delete_sql("subscriptions", "subscriptionjob"),
            reverse_sql=procrastinate_on_delete_sql(
                "subscriptions", "subscriptionjob", reverse=True
            ),
        ),
        migrations.RunSQL(
            sql=procrastinate_on_delete_sql("subscriptions", "subscriptiontask"),
            reverse_sql=procrastinate_on_delete_sql(
                "subscriptions", "subscriptiontask", reverse=True
            ),
        ),
    ]
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `uv run pytest radis/labels/tests/test_models.py::test_raw_queue_row_delete_nulls_labeling_task_fk radis/subscriptions/tests/test_constraints.py::test_raw_queue_row_delete_nulls_subscription_task_fk -v`
Expected: PASS. (If the test DB is cached, add `--create-db` once.)

- [ ] **Step 7: Verify no auto-migrations are pending**

Run: `uv run ./manage.py makemigrations --check --dry-run`
Expected: "No changes detected".

- [ ] **Step 8: Commit**

```bash
git add radis/labels/migrations/0002_procrastinate_on_delete.py \
        radis/subscriptions/migrations/0012_procrastinate_on_delete.py \
        radis/labels/tests/test_models.py radis/subscriptions/tests/test_constraints.py
git commit -m "fix(core): make queued_job FK ON DELETE SET NULL at DB level for labels and subscriptions"
```

---

### Task 2: `update_job_state()` all-canceled branch

**Files:**
- Modify: `radis/core/models.py:128-130`
- Test: `radis/core/tests/test_models.py` (append)

**Interfaces:**
- Consumes: nothing new.
- Produces: `AnalysisJob.update_job_state()` no longer raises when every task is `CANCELED` and the job is not `CANCELING`; it settles the job to `CANCELED` with message `"All tasks canceled."` and returns `False` without sending mail. Task 4's sweep calls this method on affected jobs.

- [ ] **Step 1: Write the failing test**

Append to `radis/core/tests/test_models.py` (add any missing imports: `from unittest.mock import patch`, `from adit_radis_shared.accounts.factories import UserFactory`, `from radis.core.models import AnalysisJob, AnalysisTask`, `from radis.extractions.factories import ExtractionJobFactory, ExtractionTaskFactory`):

```python
@pytest.mark.django_db
def test_update_job_state_all_tasks_canceled_without_canceling_job():
    user = UserFactory.create()
    job = ExtractionJobFactory.create(
        owner=user, status=AnalysisJob.Status.IN_PROGRESS, send_finished_mail=True
    )
    ExtractionTaskFactory.create(job=job, status=AnalysisTask.Status.CANCELED)

    with patch.object(type(job), "_send_job_finished_mail") as mock_mail:
        result = job.update_job_state()

    job.refresh_from_db()
    assert result is False
    assert job.status == AnalysisJob.Status.CANCELED
    assert job.message == "All tasks canceled."
    mock_mail.assert_not_called()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest radis/core/tests/test_models.py::test_update_job_state_all_tasks_canceled_without_canceling_job -v`
Expected: FAIL with `AssertionError: Invalid task status of …`.

- [ ] **Step 3: Implement**

In `radis/core/models.py`, replace:

```python
        else:
            # at least one of success, warnings or failures must be > 0
            raise AssertionError(f"Invalid task status of {self}.")
```

with:

```python
        else:
            # Every task is CANCELED — reachable when a task of an already CANCELED job
            # re-fires and is canceled again. Settle the job instead of raising.
            self.status = AnalysisJob.Status.CANCELED
            self.message = "All tasks canceled."
            self.save()
            return False
```

(The `return False` skips the `ended_at`/finished-mail block below, matching the CANCELING branch above which also sets no `ended_at`.)

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest radis/core/tests/test_models.py -v`
Expected: PASS (all tests in the file).

- [ ] **Step 5: Commit**

```bash
git add radis/core/models.py radis/core/tests/test_models.py
git commit -m "fix(core): settle job to CANCELED instead of raising when all tasks are canceled"
```

---

### Task 3: Atomic claim in `AnalysisTaskProcessor.start()`

**Files:**
- Modify: `radis/core/processors.py:37-51`
- Test: `radis/core/tests/test_processors.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: `start()` executes a task only after atomically flipping it `PENDING → IN_PROGRESS`; on a failed claim it logs a warning and returns (queue row consumed as no-op). The cancel branch (lines 25-35) and `assert job.status == job.Status.IN_PROGRESS` stay. Tasks 4/6's sweep relies on the processor **never** repairing.

- [ ] **Step 1: Write the failing tests**

In `radis/core/tests/test_processors.py`, **delete** `test_start_assertion_error_on_invalid_task_status` (lines 253-262) and add these three tests (imports already present in the file):

```python
@pytest.mark.django_db
def test_start_skips_task_not_pending():
    user = UserFactory.create()
    job = ExtractionJobFactory.create(owner=user, status=AnalysisJob.Status.PENDING)
    task = ExtractionTaskFactory.create(job=job, status=AnalysisTask.Status.SUCCESS)

    processor = AnalysisTaskProcessor(task)

    with patch.object(processor, "process_task") as mock_process_task:
        processor.start()  # must not raise

    task.refresh_from_db()
    assert task.status == AnalysisTask.Status.SUCCESS
    mock_process_task.assert_not_called()


@pytest.mark.django_db
def test_start_skips_stale_in_progress_task():
    user = UserFactory.create()
    job = ExtractionJobFactory.create(owner=user, status=AnalysisJob.Status.IN_PROGRESS)
    task = ExtractionTaskFactory.create(job=job, status=AnalysisTask.Status.IN_PROGRESS)

    processor = AnalysisTaskProcessor(task)

    with (
        patch.object(processor, "process_task") as mock_process_task,
        patch("radis.core.processors.logger") as mock_logger,
    ):
        processor.start()

    task.refresh_from_db()
    # Repair belongs to the sweep; the processor must leave the task untouched.
    assert task.status == AnalysisTask.Status.IN_PROGRESS
    mock_process_task.assert_not_called()
    mock_logger.warning.assert_called_once()


@pytest.mark.django_db
def test_start_cancels_in_progress_task_under_canceling_job():
    user = UserFactory.create()
    job = ExtractionJobFactory.create(owner=user, status=AnalysisJob.Status.CANCELING)
    task = ExtractionTaskFactory.create(job=job, status=AnalysisTask.Status.IN_PROGRESS)

    processor = AnalysisTaskProcessor(task)

    with (
        patch.object(processor, "process_task") as mock_process_task,
        patch.object(job, "update_job_state") as mock_update_job_state,
    ):
        processor.start()

    task.refresh_from_db()
    assert task.status == AnalysisTask.Status.CANCELED
    mock_process_task.assert_not_called()
    mock_update_job_state.assert_called_once()
```

- [ ] **Step 2: Run tests to verify the new ones fail**

Run: `uv run pytest radis/core/tests/test_processors.py -v`
Expected: `test_start_skips_task_not_pending` and `test_start_skips_stale_in_progress_task` FAIL with `AssertionError` from the old `assert task.status == task.Status.PENDING`. `test_start_cancels_in_progress_task_under_canceling_job` already PASSES (existing cancel branch).

- [ ] **Step 3: Implement the claim**

In `radis/core/processors.py`, replace this block (the assert, the job transition stays where it is, and the old task-IN_PROGRESS save):

```python
        assert task.status == task.Status.PENDING

        # When the first task is going to be processed then the
        # status of the job switches from PENDING to IN_PROGRESS
        if job.status == job.Status.PENDING:
            job.status = job.Status.IN_PROGRESS
            job.started_at = timezone.now()
            job.save()

        assert job.status == job.Status.IN_PROGRESS

        # Prepare the task itself
        task.status = AnalysisTask.Status.IN_PROGRESS
        task.started_at = timezone.now()
        task.save()
```

with:

```python
        # Atomic claim: only a task that is PENDING at this very instant may run. A stale
        # IN_PROGRESS left by a killed worker is repaired by the sweep, never here — a
        # read-then-branch would race that sweep (see the worker-crash recovery spec).
        now = timezone.now()
        claimed = type(task).objects.filter(
            pk=task.pk, status=AnalysisTask.Status.PENDING
        ).update(status=AnalysisTask.Status.IN_PROGRESS, started_at=now)
        if not claimed:
            logger.warning("Task %s was not PENDING, skipping.", task)
            return
        # Mirror the claim on the in-memory instance so the final save() writes the same values.
        task.status = AnalysisTask.Status.IN_PROGRESS
        task.started_at = now

        # When the first task is going to be processed then the
        # status of the job switches from PENDING to IN_PROGRESS
        if job.status == job.Status.PENDING:
            job.status = job.Status.IN_PROGRESS
            job.started_at = timezone.now()
            job.save()

        assert job.status == job.Status.IN_PROGRESS
```

If pyright complains about `.objects` on `type(task)` (abstract base typing), assign `task_model = type(task)` and add `# type: ignore[attr-defined]` on the `.objects` access.

- [ ] **Step 4: Run the whole file to verify everything passes**

Run: `uv run pytest radis/core/tests/test_processors.py -v`
Expected: PASS — including the untouched `test_start_job_transition_from_pending_to_in_progress` (spec test 14: claim success stamps `started_at`) and `test_start_assertion_error_on_invalid_job_status` (job assert unchanged).

- [ ] **Step 5: Commit**

```bash
git add radis/core/processors.py radis/core/tests/test_processors.py
git commit -m "feat(core): replace processor status assert with an atomic PENDING claim"
```

---

### Task 4: Settings + sweep logic (`recovery.py`)

**Files:**
- Modify: `radis/settings/base.py` (after `STALLED_JOBS_RETRY_PRIORITY = 10`, ~line 582)
- Create: `radis/core/utils/recovery.py`
- Test: `radis/core/tests/test_recovery.py` (new)

**Interfaces:**
- Consumes: `AnalysisJob`/`AnalysisTask` from `radis.core.models`; `ProcrastinateJob` (read-only model); `settings.ANALYSIS_STALLED_WORKER_GRACE_SECONDS`; `task.delay()`; `job.update_job_state()` (Task 2's non-raising version).
- Produces:
  - `sweep_stale_analysis_state() -> None` — the full sweep (Tasks 5 and 6 call it).
  - `_owner_gone_q(cutoff: datetime) -> Q` and `_resolve_stale_task(task: AnalysisTask, owner_gone: Q) -> str | None` (returns `"pending"`, `"canceled"`, or `None` on a lost race) — module-private but exercised directly by race tests.

- [ ] **Step 1: Add the settings**

In `radis/settings/base.py`, directly below `STALLED_JOBS_RETRY_PRIORITY = 10`:

```python
# Heartbeat silence before a worker counts as dead when repairing stale analysis tasks.
# Never set below 30 — Procrastinate itself declares workers stalled at 30 s.
ANALYSIS_STALLED_WORKER_GRACE_SECONDS = env.int("ANALYSIS_STALLED_WORKER_GRACE_SECONDS", default=30)

# Cron for the periodic sweep that repairs tasks left IN_PROGRESS by killed workers.
ANALYSIS_SWEEP_CRON = env.str("ANALYSIS_SWEEP_CRON", default="* * * * *")
```

- [ ] **Step 2: Write the test file (failing)**

Create `radis/core/tests/test_recovery.py`:

```python
from datetime import timedelta
from unittest.mock import patch

import pytest
from adit_radis_shared.accounts.factories import UserFactory
from django.db import connection
from django.utils import timezone
from procrastinate.contrib.django.models import ProcrastinateJob, ProcrastinateWorker

from radis.core.models import AnalysisJob, AnalysisTask
from radis.core.utils import recovery
from radis.core.utils.recovery import sweep_stale_analysis_state
from radis.extractions.factories import ExtractionJobFactory, ExtractionTaskFactory
from radis.extractions.models import ExtractionTask


@pytest.fixture(autouse=True)
def writable_procrastinate(settings):
    settings.PROCRASTINATE_READONLY_MODELS = False


def create_worker(heartbeat_age_seconds: int) -> ProcrastinateWorker:
    return ProcrastinateWorker.objects.create(
        last_heartbeat=timezone.now() - timedelta(seconds=heartbeat_age_seconds)
    )


def create_row(status: str, worker: ProcrastinateWorker | None = None) -> ProcrastinateJob:
    return ProcrastinateJob.objects.create(
        queue_name="llm",
        task_name="radis.extractions.tasks.process_extraction_task",
        priority=0,
        args={},
        status=status,
        attempts=0,
        abort_requested=False,
        worker=worker,
    )


def make_stale_task(job_status, row: ProcrastinateJob | None) -> ExtractionTask:
    user = UserFactory.create()
    job = ExtractionJobFactory.create(owner=user, status=job_status)
    return ExtractionTaskFactory.create(
        job=job, status=AnalysisTask.Status.IN_PROGRESS, queued_job=row
    )


def owner_gone_q():
    cutoff = timezone.now() - timedelta(seconds=30)
    return recovery._owner_gone_q(cutoff)


@pytest.mark.django_db
def test_orphan_under_canceling_job_is_canceled():
    # The reported bug: task IN_PROGRESS, queue row gone, job CANCELING.
    task = make_stale_task(AnalysisJob.Status.CANCELING, row=None)

    sweep_stale_analysis_state()

    task.refresh_from_db()
    task.job.refresh_from_db()
    assert task.status == AnalysisTask.Status.CANCELED
    assert task.message == "The worker processing this task was terminated."
    assert task.ended_at is not None
    assert task.job.status == AnalysisJob.Status.CANCELED


@pytest.mark.django_db
def test_orphan_under_live_job_is_requeued():
    task = make_stale_task(AnalysisJob.Status.IN_PROGRESS, row=None)

    with patch.object(ExtractionTask, "delay", autospec=True) as mock_delay:
        sweep_stale_analysis_state()

    task.refresh_from_db()
    task.job.refresh_from_db()
    assert task.status == AnalysisTask.Status.PENDING
    assert task.ended_at is None
    mock_delay.assert_called_once()
    assert task.job.status == AnalysisJob.Status.PENDING  # not terminal


@pytest.mark.django_db
def test_todo_row_with_stale_worker_is_reset_without_requeue():
    row = create_row("todo", worker=create_worker(heartbeat_age_seconds=60))
    task = make_stale_task(AnalysisJob.Status.IN_PROGRESS, row=row)

    with patch.object(ExtractionTask, "delay", autospec=True) as mock_delay:
        sweep_stale_analysis_state()

    task.refresh_from_db()
    assert task.status == AnalysisTask.Status.PENDING
    mock_delay.assert_not_called()  # retry_stalled_jobs re-queued that same row


@pytest.mark.django_db
def test_doing_row_with_stale_worker_is_reset_without_requeue():
    row = create_row("doing", worker=create_worker(heartbeat_age_seconds=60))
    task = make_stale_task(AnalysisJob.Status.IN_PROGRESS, row=row)

    with patch.object(ExtractionTask, "delay", autospec=True) as mock_delay:
        sweep_stale_analysis_state()

    task.refresh_from_db()
    assert task.status == AnalysisTask.Status.PENDING
    mock_delay.assert_not_called()
    # Procrastinate's own row is never touched.
    assert ProcrastinateJob.objects.get(pk=row.pk).status == "doing"


@pytest.mark.django_db
def test_doing_row_with_fresh_worker_is_left_alone():
    # The long-LLM-batch guarantee.
    row = create_row("doing", worker=create_worker(heartbeat_age_seconds=0))
    task = make_stale_task(AnalysisJob.Status.IN_PROGRESS, row=row)

    with patch.object(ExtractionTask, "delay", autospec=True) as mock_delay:
        sweep_stale_analysis_state()

    task.refresh_from_db()
    assert task.status == AnalysisTask.Status.IN_PROGRESS
    assert task.queued_job_id == row.pk
    mock_delay.assert_not_called()


@pytest.mark.django_db
def test_doing_row_without_worker_is_treated_as_dead():
    row = create_row("doing", worker=None)
    task = make_stale_task(AnalysisJob.Status.IN_PROGRESS, row=row)

    with patch.object(ExtractionTask, "delay", autospec=True) as mock_delay:
        sweep_stale_analysis_state()

    task.refresh_from_db()
    assert task.status == AnalysisTask.Status.PENDING
    mock_delay.assert_not_called()  # the doing row still fires via retry_stalled_jobs


@pytest.mark.django_db
@pytest.mark.parametrize("row_status", ["succeeded", "failed"])
def test_terminal_row_is_resolved_like_an_orphan(row_status):
    row = create_row(row_status)
    task = make_stale_task(AnalysisJob.Status.IN_PROGRESS, row=row)

    with patch.object(ExtractionTask, "delay", autospec=True) as mock_delay:
        sweep_stale_analysis_state()

    task.refresh_from_db()
    assert task.status == AnalysisTask.Status.PENDING
    mock_delay.assert_called_once()


@pytest.mark.django_db
@pytest.mark.parametrize(
    "task_status", [AnalysisTask.Status.PENDING, AnalysisTask.Status.SUCCESS]
)
def test_non_in_progress_tasks_are_never_touched(task_status):
    user = UserFactory.create()
    job = ExtractionJobFactory.create(owner=user, status=AnalysisJob.Status.IN_PROGRESS)
    task = ExtractionTaskFactory.create(job=job, status=task_status, queued_job=None)

    with patch.object(ExtractionTask, "delay", autospec=True) as mock_delay:
        sweep_stale_analysis_state()

    task.refresh_from_db()
    assert task.status == task_status
    mock_delay.assert_not_called()


@pytest.mark.django_db
def test_resolve_twice_changes_nothing_the_second_time():
    task = make_stale_task(AnalysisJob.Status.IN_PROGRESS, row=None)
    stale_copy = ExtractionTask.objects.get(pk=task.pk)  # second sweep's stale candidate

    with patch.object(ExtractionTask, "delay", autospec=True) as mock_delay:
        assert recovery._resolve_stale_task(task, owner_gone_q()) == "pending"
        assert recovery._resolve_stale_task(stale_copy, owner_gone_q()) is None

    assert mock_delay.call_count == 1


@pytest.mark.django_db
def test_requeue_decision_uses_fresh_read_not_snapshot():
    # The row exists (todo) at candidate-selection time, but fires and is deleted before
    # the resolve step runs. The snapshot says "row exists, don't re-queue" — the fresh
    # read must notice the row is gone and call delay(), else the task is lost forever.
    row = create_row("todo", worker=create_worker(heartbeat_age_seconds=60))
    task = make_stale_task(AnalysisJob.Status.IN_PROGRESS, row=row)
    task = ExtractionTask.objects.select_related("queued_job").get(pk=task.pk)  # snapshot

    with connection.cursor() as cursor:  # raw delete, like Procrastinate's --delete-jobs
        cursor.execute("DELETE FROM procrastinate_jobs WHERE id = %s", [row.pk])

    with patch.object(ExtractionTask, "delay", autospec=True) as mock_delay:
        assert recovery._resolve_stale_task(task, owner_gone_q()) == "pending"

    mock_delay.assert_called_once()


@pytest.mark.django_db
def test_resolve_declines_when_row_is_doing_under_fresh_worker():
    # Owner-gone is re-checked inside the conditional UPDATE: a candidate whose row a live
    # worker claimed in the meantime must not be flipped.
    row = create_row("doing", worker=create_worker(heartbeat_age_seconds=0))
    task = make_stale_task(AnalysisJob.Status.IN_PROGRESS, row=row)

    with patch.object(ExtractionTask, "delay", autospec=True) as mock_delay:
        assert recovery._resolve_stale_task(task, owner_gone_q()) is None

    task.refresh_from_db()
    assert task.status == AnalysisTask.Status.IN_PROGRESS
    mock_delay.assert_not_called()
```

Note on `test_requeue_decision_uses_fresh_read_not_snapshot`: the raw SQL delete relies on the extractions DB-level `ON DELETE SET NULL` (already migrated), which nulls the task's `queued_job_id` in the DB while the in-memory snapshot still holds it — exactly what happens in production mid-sweep.

- [ ] **Step 3: Run tests to verify they fail**

Run: `uv run pytest radis/core/tests/test_recovery.py -v`
Expected: FAIL with `ModuleNotFoundError`/`ImportError` (recovery module does not exist).

- [ ] **Step 4: Implement `radis/core/utils/recovery.py`**

```python
import logging
from datetime import datetime, timedelta

from django.apps import apps
from django.conf import settings
from django.db.models import Q
from django.utils import timezone
from procrastinate.contrib.django.models import ProcrastinateJob
from procrastinate.jobs import Status

from radis.core.models import AnalysisJob, AnalysisTask

logger = logging.getLogger(__name__)

# Row statuses under which no worker is executing the row. ABORTING is documented as
# legacy and unused; a row in any status other than "doing" is not being run by anyone.
_INACTIVE_ROW_STATUSES = [
    Status.TODO.value,
    Status.SUCCEEDED.value,
    Status.FAILED.value,
    Status.CANCELLED.value,
    Status.ABORTED.value,
]

_LIVE_ROW_STATUSES = [Status.TODO.value, Status.DOING.value]

_TERMINAL_JOB_STATUSES = (
    AnalysisJob.Status.CANCELED,
    AnalysisJob.Status.SUCCESS,
    AnalysisJob.Status.WARNING,
    AnalysisJob.Status.FAILURE,
)


def _analysis_task_models() -> list[type[AnalysisTask]]:
    return [m for m in apps.get_models() if issubclass(m, AnalysisTask)]


def _owner_gone_q(cutoff: datetime) -> Q:
    """The worker that was running the task is gone.

    Positive disjunctions on purpose: with a nullable join, an .exclude() of the
    live case evaluates to NULL for orphans and silently drops them.
    """
    return (
        # queue row deleted (Procrastinate runs with --delete-jobs=always)
        Q(queued_job__isnull=True)
        # queue row is not being run by anyone: already re-queued, or finished
        | Q(queued_job__status__in=_INACTIVE_ROW_STATUSES)
        # queue row says doing, but its worker row was pruned
        | Q(queued_job__status=Status.DOING.value, queued_job__worker__isnull=True)
        # queue row says doing, but its worker stopped sending heartbeats
        | Q(queued_job__status=Status.DOING.value, queued_job__worker__last_heartbeat__lt=cutoff)
    )


def _resolve_stale_task(task: AnalysisTask, owner_gone: Q) -> str | None:
    """Repair one stale candidate. Returns the outcome, or None if the race was lost."""
    model = type(task)
    job = task.job

    if job.status in (AnalysisJob.Status.CANCELING, AnalysisJob.Status.CANCELED):
        new_status = AnalysisTask.Status.CANCELED
        ended_at = timezone.now()
    else:
        new_status = AnalysisTask.Status.PENDING
        ended_at = None

    stale_job_id = task.queued_job_id  # capture before the update nulls it

    # Conditional UPDATE re-checks status AND owner-gone at execution time: the other
    # container sweeps concurrently, and a live worker may claim the task at any moment.
    updated = (
        model.objects.filter(pk=task.pk, status=AnalysisTask.Status.IN_PROGRESS)
        .filter(owner_gone)
        .update(
            status=new_status,
            message="The worker processing this task was terminated.",
            ended_at=ended_at,
            queued_job_id=None,
        )
    )
    if not updated:
        return None  # another sweep won, or a live worker claimed the task meanwhile

    if new_status == AnalysisTask.Status.PENDING:
        # Fresh read — never the candidate snapshot: the row can fire, fail its claim and
        # be deleted mid-sweep, and a PENDING task with no row is invisible to every
        # future sweep. Re-queue only when no live row remains.
        row_alive = (
            stale_job_id is not None
            and ProcrastinateJob.objects.filter(
                pk=stale_job_id, status__in=_LIVE_ROW_STATUSES
            ).exists()
        )
        if not row_alive:
            task.refresh_from_db()  # delay() does a full save; don't write back stale fields
            task.delay()

    return "pending" if new_status == AnalysisTask.Status.PENDING else "canceled"


def sweep_stale_analysis_state() -> None:
    """Repair tasks left IN_PROGRESS by a killed worker, across all AnalysisTask models."""
    cutoff = timezone.now() - timedelta(seconds=settings.ANALYSIS_STALLED_WORKER_GRACE_SECONDS)
    owner_gone = _owner_gone_q(cutoff)

    summary: list[str] = []
    affected_jobs: dict[tuple[str, int], AnalysisJob] = {}

    for model in _analysis_task_models():
        pending = canceled = 0
        candidates = (
            model.objects.filter(status=AnalysisTask.Status.IN_PROGRESS)
            .filter(owner_gone)
            .select_related("job", "queued_job", "queued_job__worker")
        )
        for task in candidates:
            outcome = _resolve_stale_task(task, owner_gone)
            if outcome == "pending":
                pending += 1
            elif outcome == "canceled":
                canceled += 1
            else:
                continue
            affected_jobs[(task.job._meta.label, task.job.pk)] = task.job

        total = pending + canceled
        if total:
            summary.append(f"{model.__name__} {total} ({pending} pending, {canceled} canceled)")
        else:
            summary.append(f"{model.__name__} 0")

    for job in affected_jobs.values():
        job.refresh_from_db()
        if job.status not in _TERMINAL_JOB_STATUSES:
            job.update_job_state()

    # One summary line: the sweep runs at container boot, and a large recovery must not
    # bury the worker's startup output.
    logger.info("Swept stale analysis state: %s", ", ".join(summary))
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest radis/core/tests/test_recovery.py -v`
Expected: PASS (all 12).

- [ ] **Step 6: Commit**

```bash
git add radis/settings/base.py radis/core/utils/recovery.py radis/core/tests/test_recovery.py
git commit -m "feat(core): sweep that repairs analysis tasks left IN_PROGRESS by killed workers"
```

---

### Task 5: `sweep_stale_tasks` management command

**Files:**
- Create: `radis/core/management/commands/sweep_stale_tasks.py` (the `management/commands/` package dirs may need creating with empty `__init__.py` files — check whether `radis/core/management/` already exists first)
- Test: `radis/core/tests/test_recovery.py` (append)

**Interfaces:**
- Consumes: `sweep_stale_analysis_state` from Task 4.
- Produces: `./manage.py sweep_stale_tasks` — always exits 0. Task 7's compose commands call it in front of `bg_worker`.

- [ ] **Step 1: Write the failing test**

Append to `radis/core/tests/test_recovery.py` (add `from django.core.management import call_command` to its imports):

```python
@pytest.mark.django_db
def test_sweep_command_exits_zero_when_sweep_raises():
    # The command gates worker boot via `&&`; a failed repair must never stop the worker.
    with patch(
        "radis.core.management.commands.sweep_stale_tasks.sweep_stale_analysis_state",
        side_effect=RuntimeError("boom"),
    ):
        call_command("sweep_stale_tasks")  # must not raise
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest radis/core/tests/test_recovery.py::test_sweep_command_exits_zero_when_sweep_raises -v`
Expected: FAIL — unknown command "sweep_stale_tasks".

- [ ] **Step 3: Implement the command**

Create `radis/core/management/commands/sweep_stale_tasks.py` (mirroring the terse style of `adit_radis_shared/common/management/commands/retry_stalled_jobs.py`):

```python
import logging

from django.core.management.base import BaseCommand

from radis.core.utils.recovery import sweep_stale_analysis_state

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Repair analysis tasks left IN_PROGRESS by a killed worker."

    def handle(self, *args, **options):
        self.stdout.write("Sweeping stale analysis tasks... ", ending="")
        self.stdout.flush()

        # Runs in front of bg_worker joined by `&&` — must never exit non-zero. A repair
        # that did not happen is recoverable; a worker that will not boot is not.
        try:
            sweep_stale_analysis_state()
        except Exception:
            logger.exception("Sweeping stale analysis tasks failed.")
            self.stdout.write("failed (see logs)")
        else:
            self.stdout.write("done")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest radis/core/tests/test_recovery.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add radis/core/management radis/core/tests/test_recovery.py
git commit -m "feat(core): sweep_stale_tasks management command for worker startup"
```

---

### Task 6: Periodic sweep task

**Files:**
- Create: `radis/core/tasks.py` (Procrastinate autodiscovers `tasks` modules of installed apps)
- Test: `radis/core/tests/test_tasks.py` (new)

**Interfaces:**
- Consumes: `sweep_stale_analysis_state` (Task 4), `settings.ANALYSIS_SWEEP_CRON` (Task 4).
- Produces: periodic Procrastinate task `radis.core.tasks.sweep_stale_tasks_periodic` on the default queue with `queueing_lock="sweep_stale_tasks"`.

- [ ] **Step 1: Write the failing tests**

Create `radis/core/tests/test_tasks.py`:

```python
from django.conf import settings
from procrastinate.contrib.django import app


def test_periodic_sweep_task_calls_sweep(monkeypatch):
    calls: list[int] = []
    monkeypatch.setattr("radis.core.tasks.sweep_stale_analysis_state", lambda: calls.append(1))

    from radis.core.tasks import sweep_stale_tasks_periodic

    sweep_stale_tasks_periodic(timestamp=0)
    assert calls == [1]


def test_periodic_sweep_task_registered_with_cron():
    registered = [
        pt
        for pt in app.periodic_registry.periodic_tasks.values()
        if pt.task.name == "radis.core.tasks.sweep_stale_tasks_periodic"
    ]
    assert len(registered) == 1
    assert registered[0].cron == settings.ANALYSIS_SWEEP_CRON
    assert registered[0].task.queueing_lock == "sweep_stale_tasks"
```

(No `django_db` marker — neither test touches the database.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest radis/core/tests/test_tasks.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'radis.core.tasks'` / empty `registered`.

- [ ] **Step 3: Implement `radis/core/tasks.py`**

```python
import logging

from django.conf import settings
from procrastinate.contrib.django import app

from radis.core.utils.recovery import sweep_stale_analysis_state

logger = logging.getLogger(__name__)


@app.periodic(cron=settings.ANALYSIS_SWEEP_CRON)
@app.task(queueing_lock="sweep_stale_tasks")
def sweep_stale_tasks_periodic(timestamp: int) -> None:
    # Unlike the startup command this may raise freely: a failed tick just logs, and the
    # queueing_lock prevents pileup.
    sweep_stale_analysis_state()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest radis/core/tests/test_tasks.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add radis/core/tasks.py radis/core/tests/test_tasks.py
git commit -m "feat(core): periodic sweep of stale analysis tasks (ANALYSIS_SWEEP_CRON)"
```

---

### Task 7: Compose worker start commands

**Files:**
- Modify: `docker-compose.prod.yml` (`default_worker` and `llm_worker` services, lines ~57-76)
- Modify: `docker-compose.dev.yml` (`default_worker` and `llm_worker` services, lines ~63-79)

**Interfaces:**
- Consumes: `sweep_stale_tasks` command (Task 5); `init.local`/`web.local` hostnames from `docker-compose.base.yml`.
- Produces: workers wait for migrations (`init`/`web`) and sweep before `bg_worker`. The wait also guarantees `retry_stalled_jobs` (run inside `init`/`web` before their port opens) has already re-queued stalled rows by sweep time.

- [ ] **Step 1: Update `docker-compose.prod.yml`**

`default_worker` command becomes:

```yaml
    command: >
      bash -c "
        wait-for-it -s postgres.local:5432 -t ${WAIT_POSTGRES_TIMEOUT:-180} &&
        wait-for-it -s init.local:8000 -t 300 &&
        ./manage.py sweep_stale_tasks &&
        ./manage.py bg_worker -q default
      "
```

`llm_worker` command becomes:

```yaml
    command: >
      bash -c "
        wait-for-it -s postgres.local:5432 -t ${WAIT_POSTGRES_TIMEOUT:-180} &&
        wait-for-it -s init.local:8000 -t 300 &&
        ./manage.py sweep_stale_tasks &&
        ./manage.py bg_worker -q llm
      "
```

- [ ] **Step 2: Update `docker-compose.dev.yml`**

Dev disables `init` (`profiles: never`) and runs `migrate` inside `web`, so wait on `web.local:8000` instead. Keep the existing dev flags (`-l debug`, `--autoreload`):

```yaml
    command: >
      bash -c "
        wait-for-it -s postgres.local:5432 -t ${WAIT_POSTGRES_TIMEOUT:-180} &&
        wait-for-it -s web.local:8000 -t 300 &&
        ./manage.py sweep_stale_tasks &&
        ./manage.py bg_worker -l debug -q default --autoreload
      "
```

and for `llm_worker` the same with `-q llm`.

- [ ] **Step 3: Validate compose syntax**

Run: `docker compose -f docker-compose.base.yml -f docker-compose.dev.yml config --quiet`
Expected: exit 0, no output. (If it complains about unset required env vars, run with `--env-file example.env`.)

- [ ] **Step 4: Commit**

```bash
git add docker-compose.prod.yml docker-compose.dev.yml
git commit -m "feat(core): run sweep_stale_tasks before bg_worker and wait for migrations"
```

---

### Task 8: Extractions resume safety + prep re-entry guard

**Files:**
- Modify: `radis/extractions/processors.py:29`
- Modify: `radis/extractions/tasks.py:36-50`
- Test: `radis/extractions/tests/test_processors.py` (new), `radis/extractions/tests/test_tasks.py` (append)

**Interfaces:**
- Consumes: nothing new.
- Produces: a resumed `ExtractionTask` reprocesses only unprocessed instances; a re-fired `process_extraction_job` in `PREPARING` resumes instead of raising.

- [ ] **Step 1: Write the failing tests**

Create `radis/extractions/tests/test_processors.py`:

```python
from unittest.mock import patch

import pytest
from adit_radis_shared.accounts.factories import UserFactory

from radis.core.models import AnalysisJob, AnalysisTask
from radis.extractions.factories import (
    ExtractionInstanceFactory,
    ExtractionJobFactory,
    ExtractionTaskFactory,
)
from radis.extractions.processors import ExtractionTaskProcessor


@pytest.mark.django_db
def test_resumed_task_skips_already_processed_instances():
    user = UserFactory.create()
    job = ExtractionJobFactory.create(owner=user, status=AnalysisJob.Status.IN_PROGRESS)
    task = ExtractionTaskFactory.create(job=job, status=AnalysisTask.Status.IN_PROGRESS)
    ExtractionInstanceFactory.create(task=task, is_processed=True)
    todo = ExtractionInstanceFactory.create(task=task, is_processed=False)

    with patch("radis.extractions.processors.LLMClient"):
        processor = ExtractionTaskProcessor(task)

    with patch.object(processor, "process_instance") as mock_process_instance:
        processor.process_task(task)

    assert mock_process_instance.call_count == 1
    assert mock_process_instance.call_args[0][0] == todo
```

Append to `radis/extractions/tests/test_tasks.py` (merge these imports with the existing ones):

```python
from unittest.mock import patch

import pytest
from adit_radis_shared.accounts.factories import UserFactory

from radis.core.models import AnalysisJob, AnalysisTask
from radis.extractions.factories import ExtractionJobFactory, ExtractionTaskFactory
from radis.extractions.models import ExtractionTask
from radis.extractions.tasks import process_extraction_job


@pytest.mark.django_db
def test_refired_prep_job_in_preparing_resumes_without_raising():
    user = UserFactory.create()
    job = ExtractionJobFactory.create(owner=user, status=AnalysisJob.Status.PREPARING)
    ExtractionTaskFactory.create(job=job, status=AnalysisTask.Status.PENDING)

    with patch.object(ExtractionTask, "delay", autospec=True) as mock_delay:
        process_extraction_job(int(job.pk))

    job.refresh_from_db()
    assert job.status == AnalysisJob.Status.PENDING
    mock_delay.assert_called_once()


@pytest.mark.django_db
def test_prep_job_in_unexpected_status_is_ignored():
    user = UserFactory.create()
    job = ExtractionJobFactory.create(owner=user, status=AnalysisJob.Status.SUCCESS)

    process_extraction_job(int(job.pk))  # must not raise

    job.refresh_from_db()
    assert job.status == AnalysisJob.Status.SUCCESS
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest radis/extractions/tests/test_processors.py radis/extractions/tests/test_tasks.py -v`
Expected: `test_resumed_task_skips_already_processed_instances` FAILS (both instances processed); the two prep tests FAIL with `AssertionError` from `assert job.status == ExtractionJob.Status.PENDING`.

- [ ] **Step 3: Implement**

In `radis/extractions/processors.py`, change line 29 from:

```python
                for instance in task.instances.all():
```

to:

```python
                # A resumed task (worker killed mid-batch) only pays for what is left.
                for instance in task.instances.filter(is_processed=False):
```

In `radis/extractions/tasks.py`, replace:

```python
    logger.info("Start preparing job %s", job)
    assert job.status == ExtractionJob.Status.PENDING
```

with:

```python
    logger.info("Start preparing job %s", job)

    # PENDING is the fresh-defer state; PREPARING means a worker crashed mid-preparation
    # and the row was re-fired. Anything else has nothing to prepare.
    if job.status not in (ExtractionJob.Status.PENDING, ExtractionJob.Status.PREPARING):
        logger.warning(
            "process_extraction_job called for job %s in status %s, ignoring.",
            job.pk,
            job.get_status_display(),
        )
        return
```

and extend the resume branch (`if job.tasks.exists():`) so a crash mid-prep does not strand the job in `PREPARING` (the processor's job assert requires IN_PROGRESS-able jobs):

```python
    if job.tasks.exists():
        # Resume path (re-fire after a crash). A crash mid-preparation leaves the job
        # PREPARING; restore PENDING first — tasks must never run under a PREPARING job.
        if job.status == ExtractionJob.Status.PREPARING:
            job.status = ExtractionJob.Status.PENDING
            job.save()
        tasks_to_enqueue = job.tasks.filter(status=ExtractionTask.Status.PENDING)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest radis/extractions/tests/ -v`
Expected: PASS (including pre-existing tests).

- [ ] **Step 5: Commit**

```bash
git add radis/extractions/processors.py radis/extractions/tasks.py radis/extractions/tests/
git commit -m "fix(extractions): resume-safe task processing and prep re-entry guard"
```

---

### Task 9: Subscriptions idempotent items + prep re-entry guard

**Files:**
- Modify: `radis/subscriptions/processors.py:151-157`
- Modify: `radis/subscriptions/tasks.py:37-38` and `_build_subscription_job` (~line 54)
- Test: `radis/subscriptions/tests/test_processors.py` (append), `radis/subscriptions/tests/test_tasks_build.py` (append)

**Interfaces:**
- Consumes: the existing `(subscription, report)` UniqueConstraint (already in `radis/subscriptions/models.py:113-120` — do not add another).
- Produces: `process_report` is idempotent; a re-fired `process_subscription_job` neither raises nor duplicates tasks.

- [ ] **Step 1: Write the failing tests**

Append to `radis/subscriptions/tests/test_processors.py` (its imports already include `SubscribedItem`, the factories, and `SubscriptionTaskProcessor`; add `from radis.core.models import AnalysisJob, AnalysisTask` and `from unittest.mock import patch` if missing):

```python
@pytest.mark.django_db
def test_running_same_report_twice_creates_one_subscribed_item():
    # No filter questions and no output fields: the report is accepted without any LLM call.
    subscription = SubscriptionFactory.create()
    job = SubscriptionJobFactory.create(
        subscription=subscription, status=AnalysisJob.Status.IN_PROGRESS
    )
    task = SubscriptionTaskFactory.create(job=job, status=AnalysisTask.Status.IN_PROGRESS)
    report = ReportFactory.create()

    with patch("radis.subscriptions.processors.LLMClient"):
        processor = SubscriptionTaskProcessor(task)

    processor.process_report(report, task)
    processor.process_report(report, task)

    assert SubscribedItem.objects.filter(subscription=subscription, report=report).count() == 1
```

Append to `radis/subscriptions/tests/test_tasks_build.py` (reuse its existing `_preparing_job` helper, `SubscriptionFilterProvider` import, and `subscription_site` monkeypatch pattern; add `from radis.subscriptions.models import SubscriptionJob` to imports if missing):

```python
@pytest.mark.django_db
def test_refired_prep_job_does_not_duplicate_tasks(monkeypatch):
    job = _preparing_job()
    ReportFactory.create(document_id="S-REFIRE-1")

    monkeypatch.setattr(
        subscription_site,
        "subscription_filter_provider",
        SubscriptionFilterProvider(name="f", filter=lambda _f: ["S-REFIRE-1"]),
    )
    monkeypatch.setattr(SubscriptionTask, "delay", lambda self: None, raising=True)

    process_subscription_job(int(job.pk))
    assert job.tasks.count() == 1

    # Simulate a crash after task creation but before the PENDING switch, then re-fire.
    SubscriptionJob.objects.filter(pk=job.pk).update(status=SubscriptionJob.Status.PREPARING)
    process_subscription_job(int(job.pk))

    job.refresh_from_db()
    assert job.tasks.count() == 1  # wiped and recreated, not duplicated


@pytest.mark.django_db
def test_prep_job_in_unexpected_status_is_ignored(monkeypatch):
    job = _preparing_job()
    SubscriptionJob.objects.filter(pk=job.pk).update(status=SubscriptionJob.Status.SUCCESS)

    process_subscription_job(int(job.pk))  # must not raise

    job.refresh_from_db()
    assert job.status == SubscriptionJob.Status.SUCCESS
    assert job.tasks.count() == 0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest radis/subscriptions/tests/test_processors.py radis/subscriptions/tests/test_tasks_build.py -v`
Expected: `test_refired_prep_job_does_not_duplicate_tasks` FAILS (2 tasks after re-fire); `test_prep_job_in_unexpected_status_is_ignored` FAILS with `AssertionError` (the current `assert job.status == PREPARING`); the idempotency test may already PASS thanks to the pre-existing `exists()` check — keep it, it pins the behavior against a race-window regression once `create()` becomes `get_or_create()`.

- [ ] **Step 3: Implement**

In `radis/subscriptions/processors.py`, replace:

```python
            SubscribedItem.objects.create(
                subscription=task.job.subscription,
                job=task.job,
                report=report,
                filter_results=filter_results or None,
                extraction_results=extraction_results or None,
            )
```

with:

```python
            # get_or_create + the (subscription, report) unique constraint make a resumed
            # task idempotent even when two runs race past the exists() check above.
            SubscribedItem.objects.get_or_create(
                subscription=subscription,
                report=report,
                defaults={
                    "job": task.job,
                    "filter_results": filter_results or None,
                    "extraction_results": extraction_results or None,
                },
            )
```

In `radis/subscriptions/tasks.py`, replace:

```python
    logger.info("Start processing job %s", job)
    assert job.status == SubscriptionJob.Status.PREPARING
```

with:

```python
    logger.info("Start processing job %s", job)

    # PREPARING is the only valid entry state (the launcher creates jobs PREPARING; a
    # crash mid-preparation re-fires still PREPARING). Anything else has nothing to do.
    if job.status != SubscriptionJob.Status.PREPARING:
        logger.warning(
            "process_subscription_job called for job %s in status %s, ignoring.",
            job.pk,
            job.get_status_display(),
        )
        return
```

In `_build_subscription_job`, directly after `logger.debug("Collecting tasks for job %s", job)`:

```python
    # Wipe partial tasks from a prior crashed preparation attempt (idempotent, mirroring
    # radis/labels/tasks.py). Safe: tasks are only enqueued after the switch to PENDING,
    # so none of these can be queued or running.
    job.tasks.all().delete()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest radis/subscriptions/tests/ -v`
Expected: PASS (including all pre-existing subscription tests).

- [ ] **Step 5: Commit**

```bash
git add radis/subscriptions/processors.py radis/subscriptions/tasks.py radis/subscriptions/tests/
git commit -m "fix(subscriptions): idempotent subscribed items and prep re-entry guard"
```

---

### Task 10: Labels end-to-end recovery test

**Files:**
- Test: `radis/labels/tests/test_jobs.py` (append)

**Interfaces:**
- Consumes: `sweep_stale_analysis_state` (Task 4), Task 2's `update_job_state()` drain, the `one_active_labeling_job` singleton index.
- Produces: regression pin for the originally reported bug.

- [ ] **Step 1: Write the test**

Append to `radis/labels/tests/test_jobs.py` (it already imports `LabelingJobFactory` and `LabelingJob`):

```python
@pytest.mark.django_db
def test_sweep_unblocks_canceling_labeling_job():
    # The reported bug end-to-end: worker killed mid-task, job canceled, job parks at
    # CANCELING and blocks the singleton index. The sweep must drain it unaided.
    from radis.core.models import AnalysisTask
    from radis.core.utils.recovery import sweep_stale_analysis_state
    from radis.labels.factories import LabelingTaskFactory

    job = LabelingJobFactory.create(status=LabelingJob.Status.CANCELING)
    LabelingTaskFactory.create(job=job, status=AnalysisTask.Status.IN_PROGRESS, queued_job=None)

    sweep_stale_analysis_state()

    job.refresh_from_db()
    assert job.status == LabelingJob.Status.CANCELED
    # Singleton index unblocked: a new labeling job can be created without IntegrityError.
    LabelingJobFactory.create(status=LabelingJob.Status.PENDING)
```

- [ ] **Step 2: Run it**

Run: `uv run pytest radis/labels/tests/test_jobs.py -v`
Expected: PASS (Tasks 2 and 4 are already in place).

- [ ] **Step 3: Commit**

```bash
git add radis/labels/tests/test_jobs.py
git commit -m "test(labels): end-to-end recovery of a labeling job wedged in CANCELING"
```

---

### Task 11: Documentation

**Files:**
- Modify: `KNOWLEDGE.md` ("Recovering a job stuck in CANCELING" section, lines ~55-72)
- Modify: `AGENTS.md` (env-var section, after the `LABELING_SCAN_CRON` bullet at line ~118; `CLAUDE.md` is a symlink to it — edit only `AGENTS.md`)
- Modify: `example.env` (after the auto-labeling block)

**Interfaces:** none — prose only.

- [ ] **Step 1: Rewrite the KNOWLEDGE.md section**

Replace the section heading and intro so recovery is documented as automatic, and demote the shell snippet. New text (keep the existing code block as the "immediate manual recovery" fallback):

```markdown
### Recovering a job stuck in CANCELING

Only one labeling job may be active at a time, and `CANCELING` counts as active — so a job wedged
in `CANCELING` blocks all future backfills and scan ticks. It happens when a worker dies mid-task:
the task freezes at `IN_PROGRESS`, and cancel then waits for it forever.

Recovery is automatic: a sweep repairs stale tasks at every worker-container start and
periodically in steady state (`ANALYSIS_SWEEP_CRON`, default every minute). A worker must be
running on the `default` queue for the periodic sweep to tick; after a full outage the startup
sweep covers it. Expect the job to drain to `CANCELED` within about a minute of the worker dying
(30 s heartbeat grace + one sweep tick).

For immediate manual recovery (or when no worker can run at all), use `uv run cli shell`:
```

then keep the existing Python snippet and the closing caveat paragraph unchanged.

- [ ] **Step 2: Add the env vars to AGENTS.md**

After the `LABELING_SCAN_CRON` bullet, add:

```markdown
Worker-crash recovery (`radis.core`):

- `ANALYSIS_STALLED_WORKER_GRACE_SECONDS`: Heartbeat silence before a worker counts as dead when
  repairing stale analysis tasks (default `30`; must never be set below 30 — Procrastinate itself
  declares workers stalled at 30 s).
- `ANALYSIS_SWEEP_CRON`: Cron for the periodic sweep that repairs tasks left `IN_PROGRESS` by
  killed workers (default `* * * * *`).
```

- [ ] **Step 3: Add the env vars to example.env**

After the auto-labeling block:

```bash
# Recovery of analysis tasks left IN_PROGRESS by a killed worker.
# The grace period must never be set below 30 (Procrastinate's own stall threshold).
ANALYSIS_STALLED_WORKER_GRACE_SECONDS=30
ANALYSIS_SWEEP_CRON=* * * * *
```

- [ ] **Step 4: Commit**

```bash
git add KNOWLEDGE.md AGENTS.md example.env
git commit -m "docs: automatic worker-crash recovery and its env vars"
```

---

### Task 12: Full verification

- [ ] **Step 1: Targeted suites**

Run: `uv run pytest radis/core/tests/ radis/extractions/tests/ radis/subscriptions/tests/ radis/labels/tests/ -v`
Expected: PASS.

- [ ] **Step 2: Full test suite**

Run: `uv run cli test`
Expected: PASS.

- [ ] **Step 3: Migrations check**

Run: `uv run ./manage.py makemigrations --check --dry-run`
Expected: "No changes detected" — the only new migrations are the two hand-written `RunSQL` ones.

- [ ] **Step 4: Lint**

Run: `uv run cli lint`
Expected: clean. Fix any findings and re-run.

- [ ] **Step 5: Commit any lint fixes**

```bash
git add -A && git commit -m "chore: lint fixes for worker-crash recovery" || echo "nothing to fix"
```

---

## Manual verification (operator, after merge / on dev compose)

Not part of the automated plan — from the spec's Verification section:

1. **The reported bug:** start a labeling backfill, `docker kill -s KILL` the `llm_worker` mid-task, cancel the job (parks at `CANCELING`), restart the worker. The job must reach `CANCELED` unaided and a new labeling job must be startable, with no shell recovery.
2. **Resume:** repeat without cancelling — once with a restart delayed past 30 s (startup sweep), once with an immediate restart (periodic sweep: either it repairs before the re-queued row fires, or the row no-ops on a failed claim and the next tick repairs the orphan). The task must run again and the job complete in both.
