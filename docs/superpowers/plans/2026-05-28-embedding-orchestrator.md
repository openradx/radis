# Embedding Orchestrator Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the `post_save`-driven `embed_reports` task and `backfill_embeddings` command with a periodic `EmbeddingJob`/`EmbeddingTask` orchestrator that batches pending embeddings without per-API-call job amplification.

**Architecture:** Mirror `ExtractionJob`/`ExtractionTask` (`radis/extractions/tasks.py:32`) and `subscription_launcher` (`radis/subscriptions/tasks.py:115`). A periodic `embedding_launcher` on `default` queue creates one `EmbeddingJob` (system-owned) per drain; `process_embedding_job` (also `default`) batches `ReportSearchVector` rows with `embedding IS NULL` into `EmbeddingTask` rows and dispatches them; `process_embedding_task` (on `embeddings` queue) calls `EmbeddingClient`, `bulk_update`s the vectors, and rolls status up via `AnalysisJob.update_job_state`.

**Tech Stack:** Django 5.1, Procrastinate (periodic tasks + `queueing_lock`), pgvector, pytest-django.

**Spec:** `docs/superpowers/specs/2026-05-15-hybrid-search-design.md` §6.

**Branch:** `feat/hybrid-search` (continue here; no worktree required).

---

## File Structure

**Files to create:**

| Path | Responsibility |
|---|---|
| `radis/pgsearch/migrations/0004_embedding_job_task.py` | Schema migration for `EmbeddingJob`, `EmbeddingTask`, `EmbeddingTask.reports` M2M |
| `radis/pgsearch/migrations/0005_system_user.py` | Data migration that idempotently creates the system user |
| `radis/pgsearch/tests/test_models_embedding.py` | Model-level tests: status defaults, owner FK, M2M |
| `radis/pgsearch/tests/test_embedding_launcher.py` | Unit tests for `embedding_launcher` |
| `radis/pgsearch/tests/test_process_embedding_job.py` | Unit tests for `process_embedding_job` |
| `radis/pgsearch/tests/test_process_embedding_task.py` | Unit tests for `process_embedding_task` |
| `radis/pgsearch/tests/test_migrations_system_user.py` | Test for the data migration |

**Files to modify:**

| Path | Change |
|---|---|
| `radis/settings/base.py:341-365` | Add `EMBEDDING_DRAIN_CRON`, `EMBEDDING_SYSTEM_USERNAME`; remove `EMBEDDING_BACKFILL_PRIORITY` (last) |
| `radis/pgsearch/models.py` | Add `EmbeddingJob` and `EmbeddingTask` model classes |
| `radis/pgsearch/tasks.py` | Replace contents: add `embedding_launcher`, `process_embedding_job`, `process_embedding_task`; remove `embed_reports` and `enqueue_embed_reports` |
| `radis/pgsearch/signals.py` | Remove `enqueue_report_embedding` receiver (lines 19-23); keep the FTS receiver |
| `radis/pgsearch/tests/test_signals.py` | Delete the two embedding-signal tests; the file becomes empty and is deleted |
| `docker-compose.dev.yml:85-92` | Add `--concurrency 4` to `embeddings_worker` command |
| `docker-compose.prod.yml:80-88` | Add `--concurrency 4` to `embeddings_worker` command |

**Files to delete:**

| Path | Reason |
|---|---|
| `radis/pgsearch/management/commands/backfill_embeddings.py` | Replaced by `embedding_launcher.defer()` from a shell |
| `radis/pgsearch/tests/test_backfill_command.py` | Tests for the deleted command |
| `radis/pgsearch/tests/test_embed_reports_task.py` | Tests for the deleted `embed_reports` task |
| `radis/pgsearch/tests/test_signals.py` | Whole file is deleted once the embedding tests are removed |

---

## Task 1: Add new settings (additive only)

**Files:**
- Modify: `radis/settings/base.py:341-365`
- Modify: `example.env`

`EMBEDDING_BACKFILL_PRIORITY` stays for now — it is removed in Task 10 after every caller is gone.

- [ ] **Step 1: Add `EMBEDDING_DRAIN_CRON` and `EMBEDDING_SYSTEM_USERNAME` to settings**

Edit `radis/settings/base.py`. Add after line 347 (after `EMBEDDING_DIM = env.int(...)`):

```python
EMBEDDING_DRAIN_CRON = env.str("EMBEDDING_DRAIN_CRON", default="0 2 * * *")
```

Add after line 360 (the `EMBEDDING_BACKFILL_PRIORITY` line):

```python
EMBEDDING_SYSTEM_USERNAME = "system"
```

- [ ] **Step 2: Document the env var in `example.env`**

Append to the `EMBEDDING_*` block in `example.env`:

```
# Cron expression for the embedding orchestrator. Default nightly at 02:00.
# Use "*/15 * * * *" for more aggressive dev draining.
EMBEDDING_DRAIN_CRON=0 2 * * *
```

- [ ] **Step 3: Verify Django config loads**

Run: `uv run cli shell -c "from django.conf import settings; print(settings.EMBEDDING_DRAIN_CRON, settings.EMBEDDING_SYSTEM_USERNAME)"`
Expected: prints `0 2 * * * system`

- [ ] **Step 4: Commit**

```bash
git add radis/settings/base.py example.env
git commit -m "feat(pgsearch): add EMBEDDING_DRAIN_CRON and EMBEDDING_SYSTEM_USERNAME settings"
```

---

## Task 2: Add `EmbeddingJob` and `EmbeddingTask` models

**Files:**
- Modify: `radis/pgsearch/models.py`
- Create: `radis/pgsearch/migrations/0004_embedding_job_task.py`
- Create: `radis/pgsearch/tests/test_models_embedding.py`

- [ ] **Step 1: Write the failing model tests**

Create `radis/pgsearch/tests/test_models_embedding.py`:

```python
import pytest
from django.contrib.auth import get_user_model

from radis.pgsearch.models import EmbeddingJob, EmbeddingTask
from radis.reports.factories import ReportFactory

User = get_user_model()
pytestmark = pytest.mark.django_db


def _system_user() -> "User":
    return User.objects.create(username="system", is_active=False)


def test_embedding_job_defaults():
    job = EmbeddingJob.objects.create(owner=_system_user())
    assert job.status == EmbeddingJob.Status.UNVERIFIED
    assert job.urgent is False
    assert job.send_finished_mail is False
    assert job.queued_job_id is None


def test_embedding_task_links_to_reports():
    job = EmbeddingJob.objects.create(owner=_system_user())
    reports = [ReportFactory.create() for _ in range(3)]
    task = EmbeddingTask.objects.create(job=job)
    task.reports.set(reports)
    assert task.status == EmbeddingTask.Status.PENDING
    assert set(task.reports.values_list("pk", flat=True)) == {r.pk for r in reports}
    assert task.attempts == 0
    assert task.queued_job_id is None
```

- [ ] **Step 2: Run tests — expect ImportError**

Run: `uv run pytest radis/pgsearch/tests/test_models_embedding.py -v`
Expected: FAIL — `ImportError: cannot import name 'EmbeddingJob'`

- [ ] **Step 3: Add models to `radis/pgsearch/models.py`**

Append to `radis/pgsearch/models.py`:

```python
from django.urls import reverse
from procrastinate.contrib.django import app
from procrastinate.contrib.django.models import ProcrastinateJob

from radis.core.models import AnalysisJob, AnalysisTask


class EmbeddingJob(AnalysisJob):
    default_priority = settings.EMBEDDING_INDEX_PRIORITY
    urgent_priority = settings.EMBEDDING_INDEX_PRIORITY

    queued_job_id: int | None
    queued_job = models.OneToOneField(
        ProcrastinateJob, null=True, on_delete=models.SET_NULL, related_name="+"
    )

    tasks: models.QuerySet["EmbeddingTask"]

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"EmbeddingJob [{self.pk}]"

    def delay(self) -> None:
        queued_job_id = app.configure_task(
            "radis.pgsearch.tasks.process_embedding_job",
            allow_unknown=False,
            priority=self.default_priority,
        ).defer(job_id=self.pk)
        self.queued_job_id = queued_job_id
        self.save()


class EmbeddingTask(AnalysisTask):
    job = models.ForeignKey[EmbeddingJob](
        EmbeddingJob, on_delete=models.CASCADE, related_name="tasks"
    )
    reports = models.ManyToManyField(Report, related_name="embedding_tasks")

    def delay(self) -> None:
        queued_job_id = app.configure_task(
            "radis.pgsearch.tasks.process_embedding_task",
            allow_unknown=False,
            priority=settings.EMBEDDING_INDEX_PRIORITY,
        ).defer(task_id=self.pk)
        self.queued_job_id = queued_job_id
        self.save()
```

- [ ] **Step 4: Generate the migration**

Run: `uv run cli shell -c "from django.core.management import call_command; call_command('makemigrations', 'pgsearch', name='embedding_job_task')"`
Expected: creates `radis/pgsearch/migrations/0004_embedding_job_task.py` containing `CreateModel` operations for `EmbeddingJob`, `EmbeddingTask`, and the M2M through-table.

- [ ] **Step 5: Apply the migration and re-run tests**

Run: `uv run cli shell -c "from django.core.management import call_command; call_command('migrate', 'pgsearch')"`
Then: `uv run pytest radis/pgsearch/tests/test_models_embedding.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add radis/pgsearch/models.py radis/pgsearch/migrations/0004_embedding_job_task.py radis/pgsearch/tests/test_models_embedding.py
git commit -m "feat(pgsearch): add EmbeddingJob and EmbeddingTask models"
```

---

## Task 3: Create the system user via data migration

**Files:**
- Create: `radis/pgsearch/migrations/0005_system_user.py`
- Create: `radis/pgsearch/tests/test_migrations_system_user.py`

- [ ] **Step 1: Write the failing migration test**

Create `radis/pgsearch/tests/test_migrations_system_user.py`:

```python
import pytest
from django.contrib.auth import get_user_model

User = get_user_model()


@pytest.mark.django_db
def test_system_user_exists_after_migrations():
    user = User.objects.get(username="system")
    assert user.is_active is False
    assert not user.has_usable_password()


@pytest.mark.django_db
def test_creating_system_user_twice_is_a_noop():
    from radis.pgsearch.migrations import _system_user_helper

    before = User.objects.filter(username="system").count()
    _system_user_helper.create_system_user_idempotent(User)
    after = User.objects.filter(username="system").count()
    assert before == after == 1
```

- [ ] **Step 2: Run tests — expect failure**

Run: `uv run pytest radis/pgsearch/tests/test_migrations_system_user.py -v`
Expected: FAIL — system user does not exist yet OR ImportError on `_system_user_helper`.

- [ ] **Step 3: Create the helper module**

Create `radis/pgsearch/migrations/_system_user_helper.py`:

```python
from django.conf import settings


def create_system_user_idempotent(user_model) -> None:
    username = settings.EMBEDDING_SYSTEM_USERNAME
    user, created = user_model.objects.get_or_create(
        username=username,
        defaults={"is_active": False},
    )
    if created:
        user.set_unusable_password()
        user.save()
```

- [ ] **Step 4: Create the data migration**

Create `radis/pgsearch/migrations/0005_system_user.py`:

```python
from django.conf import settings
from django.db import migrations

from radis.pgsearch.migrations._system_user_helper import create_system_user_idempotent


def forwards(apps, schema_editor):
    User = apps.get_model(*settings.AUTH_USER_MODEL.split("."))
    create_system_user_idempotent(User)


class Migration(migrations.Migration):
    dependencies = [
        ("pgsearch", "0004_embedding_job_task"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]
    operations = [migrations.RunPython(forwards, reverse_code=migrations.RunPython.noop)]
```

- [ ] **Step 5: Apply migration and run tests**

Run: `uv run cli shell -c "from django.core.management import call_command; call_command('migrate', 'pgsearch')"`
Then: `uv run pytest radis/pgsearch/tests/test_migrations_system_user.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add radis/pgsearch/migrations/0005_system_user.py radis/pgsearch/migrations/_system_user_helper.py radis/pgsearch/tests/test_migrations_system_user.py
git commit -m "feat(pgsearch): add data migration for system user"
```

---

## Task 4: Implement `process_embedding_task` (sub-task)

**Files:**
- Modify: `radis/pgsearch/tasks.py`
- Create: `radis/pgsearch/tests/test_process_embedding_task.py`

The existing `embed_reports` task and its helper stay in place for now — they are removed in Task 8. This task adds the new sub-task alongside.

- [ ] **Step 1: Write the failing tests**

Create `radis/pgsearch/tests/test_process_embedding_task.py`:

```python
from unittest.mock import MagicMock, patch

import numpy as np
import pytest
from django.contrib.auth import get_user_model

from radis.pgsearch.models import EmbeddingJob, EmbeddingTask, ReportSearchVector
from radis.pgsearch.tasks import process_embedding_task as _wrapped
from radis.pgsearch.utils.embedding_client import EmbeddingClientError
from radis.reports.factories import ReportFactory

User = get_user_model()
process_embedding_task = _wrapped.__wrapped__  # type: ignore[attr-defined]
pytestmark = pytest.mark.django_db


def _make_task() -> EmbeddingTask:
    owner = User.objects.get(username="system")
    job = EmbeddingJob.objects.create(owner=owner)
    task = EmbeddingTask.objects.create(job=job)
    reports = [ReportFactory.create() for _ in range(2)]
    task.reports.set(reports)
    return task


def _unit_vec(dim: int) -> list[float]:
    v = np.ones(dim, dtype=np.float32)
    return (v / np.linalg.norm(v)).tolist()


def test_process_embedding_task_writes_vectors_and_marks_success(settings):
    task = _make_task()
    vec = _unit_vec(settings.EMBEDDING_DIM)
    fake_client = MagicMock()
    fake_client.embed_documents.return_value = [vec, vec]
    with patch("radis.pgsearch.tasks.EmbeddingClient", return_value=fake_client):
        process_embedding_task(task.id)

    task.refresh_from_db()
    assert task.status == EmbeddingTask.Status.SUCCESS
    assert task.queued_job_id is None
    for report in task.reports.all():
        rsv = ReportSearchVector.objects.get(report=report)
        assert rsv.embedding is not None


def test_process_embedding_task_failure_sets_status_and_raises():
    task = _make_task()
    fake_client = MagicMock()
    fake_client.embed_documents.side_effect = EmbeddingClientError("boom")
    with patch("radis.pgsearch.tasks.EmbeddingClient", return_value=fake_client):
        with pytest.raises(EmbeddingClientError):
            process_embedding_task(task.id)

    task.refresh_from_db()
    assert task.status == EmbeddingTask.Status.FAILURE
    assert task.queued_job_id is None
    assert "boom" in task.message


def test_process_embedding_task_calls_update_job_state(settings):
    task = _make_task()
    vec = _unit_vec(settings.EMBEDDING_DIM)
    fake_client = MagicMock()
    fake_client.embed_documents.return_value = [vec, vec]
    with patch("radis.pgsearch.tasks.EmbeddingClient", return_value=fake_client):
        process_embedding_task(task.id)

    task.job.refresh_from_db()
    # All tasks succeeded; AnalysisJob.update_job_state rolls up to SUCCESS.
    assert task.job.status == EmbeddingJob.Status.SUCCESS
```

- [ ] **Step 2: Run tests — expect ImportError**

Run: `uv run pytest radis/pgsearch/tests/test_process_embedding_task.py -v`
Expected: FAIL — `ImportError: cannot import name 'process_embedding_task'`

- [ ] **Step 3: Add `process_embedding_task` to `radis/pgsearch/tasks.py`**

Append to `radis/pgsearch/tasks.py` (existing imports already cover `logger`, `EmbeddingClient`, `ReportSearchVector`, `app`, `django_settings`):

```python
from django.utils import timezone

from .models import EmbeddingTask
from .utils.embedding_client import EmbeddingClientError


@app.task(queue="embeddings")
def process_embedding_task(task_id: int) -> None:
    task = EmbeddingTask.objects.get(id=task_id)
    task.status = EmbeddingTask.Status.IN_PROGRESS
    task.started_at = timezone.now()
    task.attempts = task.attempts + 1
    task.save()

    client = EmbeddingClient()
    try:
        report_ids = list(task.reports.values_list("pk", flat=True))
        rsvs = list(
            ReportSearchVector.objects
            .filter(report_id__in=report_ids)
            .select_related("report")
            .only("id", "report_id", "report__body")
        )
        texts = [rsv.report.body for rsv in rsvs]
        vectors = client.embed_documents(texts)
        for rsv, vec in zip(rsvs, vectors, strict=True):
            rsv.embedding = vec
        ReportSearchVector.objects.bulk_update(rsvs, fields=["embedding"])

        task.status = EmbeddingTask.Status.SUCCESS
    except EmbeddingClientError as exc:
        logger.exception("Embedding task %s failed: %s", task_id, exc)
        task.status = EmbeddingTask.Status.FAILURE
        task.message = str(exc)
        raise
    finally:
        task.ended_at = timezone.now()
        task.queued_job_id = None
        task.save()
        task.job.update_job_state()
        client.close()
```

- [ ] **Step 4: Run tests and verify they pass**

Run: `uv run pytest radis/pgsearch/tests/test_process_embedding_task.py -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add radis/pgsearch/tasks.py radis/pgsearch/tests/test_process_embedding_task.py
git commit -m "feat(pgsearch): add process_embedding_task on embeddings queue"
```

---

## Task 5: Implement `process_embedding_job` (orchestrator)

**Files:**
- Modify: `radis/pgsearch/tasks.py`
- Create: `radis/pgsearch/tests/test_process_embedding_job.py`

- [ ] **Step 1: Write the failing tests**

Create `radis/pgsearch/tests/test_process_embedding_job.py`:

```python
from unittest.mock import patch

import pytest
from django.contrib.auth import get_user_model

from radis.pgsearch.models import EmbeddingJob, EmbeddingTask, ReportSearchVector
from radis.pgsearch.tasks import process_embedding_job as _wrapped
from radis.reports.factories import ReportFactory

User = get_user_model()
process_embedding_job = _wrapped.__wrapped__  # type: ignore[attr-defined]
pytestmark = pytest.mark.django_db


def _new_job() -> EmbeddingJob:
    owner = User.objects.get(username="system")
    return EmbeddingJob.objects.create(owner=owner, status=EmbeddingJob.Status.PREPARING)


def _make_pending_reports(n: int):
    reports = [ReportFactory.create() for _ in range(n)]
    # ReportFactory triggers the FTS post_save signal which creates ReportSearchVector
    # rows with embedding=NULL; that's exactly the pending state we want.
    return reports


def test_process_embedding_job_batches_pending_reports(settings):
    settings.EMBEDDING_BATCH_SIZE = 2
    job = _new_job()
    reports = _make_pending_reports(5)

    with patch("radis.pgsearch.models.EmbeddingTask.delay") as delay_mock:
        process_embedding_job(job.id)

    job.refresh_from_db()
    assert job.status == EmbeddingJob.Status.PENDING
    # ceil(5 / 2) = 3 tasks
    assert job.tasks.count() == 3
    # All tasks are dispatched
    assert delay_mock.call_count == 3
    # Every pending report is in exactly one task
    covered = set()
    for task in job.tasks.all():
        covered.update(task.reports.values_list("pk", flat=True))
    assert covered == {r.pk for r in reports}


def test_process_embedding_job_resume_path_only_redispatches_pending_tasks(settings):
    settings.EMBEDDING_BATCH_SIZE = 2
    job = _new_job()
    reports = _make_pending_reports(2)
    # Simulate a previous orchestrator run that created one task already.
    existing = EmbeddingTask.objects.create(job=job, status=EmbeddingTask.Status.PENDING)
    existing.reports.set(reports)
    succeeded = EmbeddingTask.objects.create(job=job, status=EmbeddingTask.Status.SUCCESS)

    with patch("radis.pgsearch.models.EmbeddingTask.delay") as delay_mock:
        process_embedding_job(job.id)

    job.refresh_from_db()
    assert job.status == EmbeddingJob.Status.PENDING
    # No new tasks created
    assert job.tasks.count() == 2
    # Only the pending one is dispatched
    assert delay_mock.call_count == 1


def test_process_embedding_job_with_no_pending_rows():
    job = _new_job()
    # No reports exist → no ReportSearchVector rows with embedding IS NULL.

    with patch("radis.pgsearch.models.EmbeddingTask.delay") as delay_mock:
        process_embedding_job(job.id)

    job.refresh_from_db()
    assert job.status == EmbeddingJob.Status.PENDING
    assert job.tasks.count() == 0
    assert delay_mock.call_count == 0
```

- [ ] **Step 2: Run tests — expect ImportError**

Run: `uv run pytest radis/pgsearch/tests/test_process_embedding_job.py -v`
Expected: FAIL — `ImportError: cannot import name 'process_embedding_job'`

- [ ] **Step 3: Add `process_embedding_job` to `radis/pgsearch/tasks.py`**

Append to `radis/pgsearch/tasks.py`:

```python
from .models import EmbeddingJob


def _create_embedding_task(job: EmbeddingJob, report_ids: list[int]) -> EmbeddingTask:
    from radis.reports.models import Report

    task = EmbeddingTask.objects.create(job=job, status=EmbeddingTask.Status.PENDING)
    task.reports.set(Report.objects.filter(pk__in=report_ids))
    return task


@app.task
def process_embedding_job(job_id: int) -> None:
    job = EmbeddingJob.objects.get(id=job_id)
    assert job.status == EmbeddingJob.Status.PREPARING

    if job.tasks.exists():
        tasks_to_enqueue = job.tasks.filter(status=EmbeddingTask.Status.PENDING)
    else:
        pending_ids_iter = (
            ReportSearchVector.objects
            .filter(embedding__isnull=True)
            .values_list("report_id", flat=True)
            .iterator(chunk_size=10_000)
        )
        batch: list[int] = []
        for report_id in pending_ids_iter:
            batch.append(int(report_id))
            if len(batch) >= django_settings.EMBEDDING_BATCH_SIZE:
                _create_embedding_task(job, batch)
                batch = []
        if batch:
            _create_embedding_task(job, batch)

        tasks_to_enqueue = job.tasks.filter(status=EmbeddingTask.Status.PENDING)

    job.status = EmbeddingJob.Status.PENDING
    job.queued_job_id = None
    job.save()

    for task in tasks_to_enqueue:
        if not task.is_queued:
            task.delay()
```

- [ ] **Step 4: Run tests and verify pass**

Run: `uv run pytest radis/pgsearch/tests/test_process_embedding_job.py -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add radis/pgsearch/tasks.py radis/pgsearch/tests/test_process_embedding_job.py
git commit -m "feat(pgsearch): add process_embedding_job orchestrator"
```

---

## Task 6: Implement `embedding_launcher` (periodic)

**Files:**
- Modify: `radis/pgsearch/tasks.py`
- Create: `radis/pgsearch/tests/test_embedding_launcher.py`

- [ ] **Step 1: Write the failing tests**

Create `radis/pgsearch/tests/test_embedding_launcher.py`:

```python
from unittest.mock import patch

import pytest
from django.contrib.auth import get_user_model

from radis.pgsearch.models import EmbeddingJob
from radis.pgsearch.tasks import embedding_launcher as _wrapped
from radis.reports.factories import ReportFactory

User = get_user_model()
embedding_launcher = _wrapped.__wrapped__  # type: ignore[attr-defined]
pytestmark = pytest.mark.django_db


def test_embedding_launcher_noop_when_job_in_flight():
    owner = User.objects.get(username="system")
    EmbeddingJob.objects.create(owner=owner, status=EmbeddingJob.Status.PREPARING)
    # Make a pending report so the second guard wouldn't short-circuit on its own.
    ReportFactory.create()

    with patch("radis.pgsearch.models.EmbeddingJob.delay") as delay_mock:
        embedding_launcher(context=None, timestamp=0)

    assert delay_mock.call_count == 0
    # No new job created.
    assert EmbeddingJob.objects.count() == 1


def test_embedding_launcher_noop_when_no_pending_rows():
    with patch("radis.pgsearch.models.EmbeddingJob.delay") as delay_mock:
        embedding_launcher(context=None, timestamp=0)

    assert delay_mock.call_count == 0
    assert EmbeddingJob.objects.count() == 0


def test_embedding_launcher_happy_path_creates_job_and_defers(
    django_capture_on_commit_callbacks,
):
    ReportFactory.create()

    with patch("radis.pgsearch.models.EmbeddingJob.delay") as delay_mock:
        with django_capture_on_commit_callbacks(execute=True):
            embedding_launcher(context=None, timestamp=0)

    assert EmbeddingJob.objects.count() == 1
    job = EmbeddingJob.objects.get()
    assert job.status == EmbeddingJob.Status.PREPARING
    assert job.owner.username == "system"
    delay_mock.assert_called_once()
```

- [ ] **Step 2: Run tests — expect ImportError**

Run: `uv run pytest radis/pgsearch/tests/test_embedding_launcher.py -v`
Expected: FAIL — `ImportError: cannot import name 'embedding_launcher'`

- [ ] **Step 3: Add `embedding_launcher` to `radis/pgsearch/tasks.py`**

Append to `radis/pgsearch/tasks.py`:

```python
from django.contrib.auth import get_user_model
from django.db import transaction


@app.periodic(cron=django_settings.EMBEDDING_DRAIN_CRON)
@app.task(
    queue="default",
    queueing_lock="embedding_launcher",
    pass_context=True,
)
def embedding_launcher(context, timestamp: int) -> None:
    in_flight = EmbeddingJob.objects.filter(
        status__in=[
            EmbeddingJob.Status.PREPARING,
            EmbeddingJob.Status.PENDING,
            EmbeddingJob.Status.IN_PROGRESS,
        ]
    ).exists()
    if in_flight:
        logger.info("EmbeddingJob already in flight; launcher tick is a no-op.")
        return

    has_pending = ReportSearchVector.objects.filter(embedding__isnull=True).exists()
    if not has_pending:
        logger.debug("No reports pending embedding; launcher tick is a no-op.")
        return

    User = get_user_model()
    system_user = User.objects.get(username=django_settings.EMBEDDING_SYSTEM_USERNAME)
    job = EmbeddingJob.objects.create(
        owner=system_user,
        status=EmbeddingJob.Status.PREPARING,
    )
    transaction.on_commit(job.delay)
```

- [ ] **Step 4: Run tests and verify pass**

Run: `uv run pytest radis/pgsearch/tests/test_embedding_launcher.py -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Verify the full pgsearch test suite still passes**

Run: `uv run pytest radis/pgsearch/ -v`
Expected: PASS for all new tests; old `test_embed_reports_task.py`, `test_backfill_command.py`, `test_signals.py` still pass since their targets aren't removed yet.

- [ ] **Step 6: Commit**

```bash
git add radis/pgsearch/tasks.py radis/pgsearch/tests/test_embedding_launcher.py
git commit -m "feat(pgsearch): add embedding_launcher periodic task"
```

---

## Task 7: Remove old `enqueue_report_embedding` signal

**Files:**
- Modify: `radis/pgsearch/signals.py:19-23`
- Delete: `radis/pgsearch/tests/test_signals.py` (the file becomes empty)

The FTS signal `create_or_update_report_search_vector` stays. The embedding signal is the only thing being removed.

- [ ] **Step 1: Remove the embedding signal receiver**

Replace `radis/pgsearch/signals.py` contents with:

```python
from django.db.models.signals import post_save
from django.dispatch import receiver

from radis.reports.models import Report

from .models import ReportSearchVector


@receiver(post_save, sender=Report)
def create_or_update_report_search_vector(sender, instance, created, **kwargs):
    if created:
        ReportSearchVector.objects.create(report=instance)
        return
    instance.search_vector.save()
```

(Removes the `transaction` and `enqueue_embed_reports` imports along with the second receiver.)

- [ ] **Step 2: Delete the signal test file**

Run: `rm radis/pgsearch/tests/test_signals.py`

- [ ] **Step 3: Run the full pgsearch test suite**

Run: `uv run pytest radis/pgsearch/ -v`
Expected: PASS for everything; `test_signals.py` no longer collected.

- [ ] **Step 4: Commit**

```bash
git add radis/pgsearch/signals.py
git rm radis/pgsearch/tests/test_signals.py
git commit -m "refactor(pgsearch): remove post_save embedding signal (replaced by orchestrator)"
```

---

## Task 8: Remove `embed_reports` task and `enqueue_embed_reports` helper

**Files:**
- Modify: `radis/pgsearch/tasks.py`
- Delete: `radis/pgsearch/tests/test_embed_reports_task.py`

At this point nothing imports `embed_reports` or `enqueue_embed_reports` (the signal was removed in Task 7; the backfill command is removed in Task 9 — but the command's import is what we now break by removing the task. The fix is to remove both in one logical step: this task removes the task, Task 9 removes the command. Order matters — do Task 8 *and* Task 9 in immediate succession so the tree never has a dangling import.

Confirm with grep before deleting:

- [ ] **Step 1: Confirm only the backfill command still imports the helper**

Run: `grep -rn "enqueue_embed_reports\|embed_reports" radis/ --include="*.py" | grep -v __pycache__`
Expected: only references in `radis/pgsearch/tasks.py`, `radis/pgsearch/management/commands/backfill_embeddings.py`, and `radis/pgsearch/tests/test_embed_reports_task.py`.

- [ ] **Step 2: Remove `embed_reports` and `enqueue_embed_reports` from `radis/pgsearch/tasks.py`**

In `radis/pgsearch/tasks.py`, delete the function definitions for `embed_reports` (the `@app.task(queue="embeddings")` block currently at lines ~37-68) and `enqueue_embed_reports` (currently at lines ~71-84). Keep `bulk_index_reports`, `enqueue_bulk_index_reports`, and all the new orchestrator code added in Tasks 4–6.

- [ ] **Step 3: Delete the old test file**

Run: `rm radis/pgsearch/tests/test_embed_reports_task.py`

- [ ] **Step 4: Verify the backfill command still imports cleanly is now expected to fail**

Run: `uv run cli shell -c "from radis.pgsearch.management.commands import backfill_embeddings"`
Expected: `ImportError: cannot import name 'enqueue_embed_reports'` — this confirms Task 9 (deleting the command) is the immediate next step.

- [ ] **Step 5: Do NOT commit yet — proceed straight to Task 9**

The tree is in a broken intermediate state. Move to Task 9 before committing.

---

## Task 9: Remove `backfill_embeddings` management command

**Files:**
- Delete: `radis/pgsearch/management/commands/backfill_embeddings.py`
- Delete: `radis/pgsearch/tests/test_backfill_command.py`

- [ ] **Step 1: Delete the command and its test**

Run:
```bash
rm radis/pgsearch/management/commands/backfill_embeddings.py
rm radis/pgsearch/tests/test_backfill_command.py
```

- [ ] **Step 2: Verify no remaining references**

Run: `grep -rn "backfill_embeddings\|enqueue_embed_reports\|embed_reports" radis/ --include="*.py" | grep -v __pycache__`
Expected: empty output.

- [ ] **Step 3: Run the full pgsearch test suite**

Run: `uv run pytest radis/pgsearch/ -v`
Expected: PASS for everything; the removed test files are no longer collected.

- [ ] **Step 4: Commit Tasks 8 + 9 together**

```bash
git add radis/pgsearch/tasks.py
git rm radis/pgsearch/tests/test_embed_reports_task.py
git rm radis/pgsearch/management/commands/backfill_embeddings.py
git rm radis/pgsearch/tests/test_backfill_command.py
git commit -m "refactor(pgsearch): remove embed_reports task and backfill_embeddings command"
```

---

## Task 10: Remove `EMBEDDING_BACKFILL_PRIORITY` setting

**Files:**
- Modify: `radis/settings/base.py:360`

- [ ] **Step 1: Confirm no remaining references**

Run: `grep -rn "EMBEDDING_BACKFILL_PRIORITY" radis/ --include="*.py" | grep -v __pycache__`
Expected: only `radis/settings/base.py:360`.

- [ ] **Step 2: Remove the setting line**

In `radis/settings/base.py`, delete the line:

```python
EMBEDDING_BACKFILL_PRIORITY = -1
```

- [ ] **Step 3: Verify Django still loads**

Run: `uv run cli shell -c "from django.conf import settings; print(settings.EMBEDDING_INDEX_PRIORITY)"`
Expected: prints `0`.

- [ ] **Step 4: Run full test suite to confirm nothing dangles**

Run: `uv run pytest radis/pgsearch/ -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add radis/settings/base.py
git commit -m "refactor(pgsearch): remove EMBEDDING_BACKFILL_PRIORITY setting"
```

---

## Task 11: Set `--concurrency 4` on the embeddings worker

**Files:**
- Modify: `docker-compose.dev.yml:85-92`
- Modify: `docker-compose.prod.yml:80-88`

The orchestrator is on `default`, so the `embeddings_worker` only runs `process_embedding_task`. Concurrency 4 saturates a typical embedding endpoint while leaving headroom; raise/lower per deployment.

- [ ] **Step 1: Update `docker-compose.dev.yml`**

Edit `docker-compose.dev.yml`. Change the `embeddings_worker` command from:

```yaml
    command: >
      bash -c "
        wait-for-it -s postgres.local:5432 -t ${WAIT_POSTGRES_TIMEOUT:-180} &&
        ./manage.py bg_worker -l debug -q embeddings --autoreload
      "
```

to:

```yaml
    command: >
      bash -c "
        wait-for-it -s postgres.local:5432 -t ${WAIT_POSTGRES_TIMEOUT:-180} &&
        ./manage.py bg_worker -l debug -q embeddings --autoreload --concurrency 4
      "
```

- [ ] **Step 2: Update `docker-compose.prod.yml`**

Edit `docker-compose.prod.yml`. Change the `embeddings_worker` command from:

```yaml
    command: >
      bash -c "
        wait-for-it -s postgres.local:5432 -t ${WAIT_POSTGRES_TIMEOUT:-180} &&
        ./manage.py bg_worker -q embeddings
      "
```

to:

```yaml
    command: >
      bash -c "
        wait-for-it -s postgres.local:5432 -t ${WAIT_POSTGRES_TIMEOUT:-180} &&
        ./manage.py bg_worker -q embeddings --concurrency 4
      "
```

- [ ] **Step 3: Validate compose syntax**

Run: `docker compose -f docker-compose.dev.yml config > /dev/null && docker compose -f docker-compose.prod.yml config > /dev/null`
Expected: exit 0, no output. (If Docker is not running locally, skip — this just confirms YAML is well-formed.)

- [ ] **Step 4: Commit**

```bash
git add docker-compose.dev.yml docker-compose.prod.yml
git commit -m "feat(infra): run embeddings_worker with --concurrency 4"
```

---

## Final verification

- [ ] **Step 1: Run lint**

Run: `uv run cli lint`
Expected: PASS (no new violations).

- [ ] **Step 2: Run the full pgsearch test suite**

Run: `uv run pytest radis/pgsearch/ -v`
Expected: PASS for every test.

- [ ] **Step 3: Run the broader app test suite**

Run: `uv run cli test`
Expected: PASS. (Pay attention to extractions/subscriptions/search since they share the AnalysisJob base.)

- [ ] **Step 4: Smoke-test in dev containers (manual)**

```bash
uv run cli compose-up -- --watch
# in another terminal:
uv run cli shell
>>> from radis.reports.factories import ReportFactory
>>> ReportFactory.create_batch(5)
>>> from radis.pgsearch.tasks import embedding_launcher
>>> embedding_launcher.defer()
# watch logs:
docker compose logs -f default_worker embeddings_worker
# verify EmbeddingJob and tasks are created and reach SUCCESS:
>>> from radis.pgsearch.models import EmbeddingJob
>>> EmbeddingJob.objects.latest("created_at").status
```

Expected: latest job's status is `SU` (SUCCESS).

- [ ] **Step 5: Push branch**

Only after the above pass.

```bash
git push -u origin feat/hybrid-search
```

---

## Spec coverage cross-check

| Spec requirement | Task |
|---|---|
| §6.2 `embeddings_worker --concurrency 4` | Task 11 |
| §6.3 priority table (no `EMBEDDING_BACKFILL_PRIORITY`) | Task 10 |
| §6.4 `EmbeddingJob`, `EmbeddingTask` models | Task 2 |
| §6.4 owner = system user via data migration | Task 3 |
| §6.5 `embedding_launcher` with `queueing_lock` + in-flight check | Task 6 |
| §6.6 `process_embedding_job` PREPARING → PENDING flow | Task 5 |
| §6.7 `process_embedding_task` on `embeddings` queue | Task 4 |
| §6.8 No post_save signal | Task 7 |
| §6.8 No `backfill_embeddings` command | Tasks 8 + 9 |
| §8.1 `EMBEDDING_DRAIN_CRON` env var | Task 1 |
| §8.2 `EMBEDDING_SYSTEM_USERNAME` constant | Task 1 |
| §10.1 unit tests for launcher/job/task | Tasks 4, 5, 6 |
