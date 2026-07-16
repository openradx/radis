# Hybrid Search — Consolidated Implementation History

**Status:** Increments 1–8 executed on `feat/hybrid-search`; the Current Plan below is pending.
**Spec:** `docs/superpowers/specs/hybrid-search.md` (the single living spec these increments built).

This document replaces the seven per-increment implementation plans (removed
2026-07-14). The full task-by-task plan texts remain in git history — retrieve any
of them with `git show f15d15a7:docs/superpowers/plans/<name>.md` (the last commit
that contains all of them). Increments are listed in execution order. The
executable plan for the current increment lives at the bottom of this file and
collapses into a history entry once executed.

## 1. Hybrid search core (planned 2026-05-28)

FTS + dense-vector retrieval fused via RRF: `ReportSearchIndex` gains an
`embedding vector(1024)` column with HNSW index, `EmbeddingClient`, deferred
embedding via Procrastinate subjobs on the dedicated `embeddings` queue and
worker, hybrid `search()`/`retrieve()` in the pgsearch provider, `embed_pending`
backfill command, admin actions. The bulk of the branch's early commits, through
the PR-cleanup pass (`b1226a51`).

## 2. Embedding client OpenAI-SDK migration (planned 2026-06-30)

Replaced the pluggable request/response backend abstraction (openai vs. Ollama
native `/api/embed`) with a single sync client over the `openai` SDK against one
OpenAI-compatible `/v1` endpoint; SDK retries disabled so RADIS owns retry
policy. Commits `f6fdad6b..79460288`, `531c2b2a`, `10d54c33`.

## 3. Embedding pipeline logging (planned 2026-06-30)

Structured INFO/WARNING logging across the pipeline: enqueue counts, per-task
start/finish with durations, admin-action audit lines (`admin.clear_embeddings:
user=… cleared N`), command summaries. What operators grep when the badge shows
something unexpected.

## 4. Embedding rate-limit gate (planned 2026-07-01)

Proactive sliding-window rate limiter (`EmbeddingRateLimitEvent` model, weight
accounting, search-priority spillover, reactive 429 handling, retry-after
parsing). Commits `9d8bc232..bdc25fd2`. **Superseded:** the proactive limiter was
replaced by reactive 429 backoff in increment 6; the never-applied migrations
were squashed away (`b198d3bf`).

## 5. Rate-limit generalization research (2026-07-01)

Research doc only (`cea9e5cb`): evaluated generalizing the pgsearch rate limiter
for LLM callers in core. Conclusion fed increment 6's design; RateLimited
auto-retry for batch tasks was deferred to a follow-up PR (port ADIT's pattern).

## 6. Shared 429 backoff → per-process gate (planned 2026-07-02)

First iteration: cross-process shared backoff state (`8a32d2a3..e4b47414`).
Replaced by a simpler per-process `RateLimitGate` (`67306dff`) — one gate per
provider (embedding vs. LLM), search queries bypass the pause with a short
budget and fall back to FTS-only. Transient (non-429) retries later unified on a
shared `radis.core.utils.rate_limit` helper, dropping the `stamina` dependency
(`4d8bdcbe`).

## 7. Backfill cancel + throughput knobs (planned 2026-07-02)

`cancel_backfill_embeddings()` helper, `embed_cancel` management command, admin
cancel-backfill button (queued backfill-priority subjobs only; cancelled jobs
are never revived), env-configurable `EMBEDDING_SUBJOB_SIZE` /
`EMBEDDING_BATCH_SIZE` / `EMBEDDINGS_WORKER_CONCURRENCY` with gentler defaults.
Commits `80bb9c37..16b42520`.

## 8. Admin badge per-subjob report counts (planned 2026-07-12)

The pipeline badge's queued/in-flight subjob counts now also show how many
reports those subjobs cover, summed DB-side via
`jsonb_array_length(args->'report_ids')`; zero counts render plain, `failed`
stays a bare subjob count. Changelist rendering tests added. Commits `8f1aad88`,
`263e4bad`.

---

## Current Plan: Report-centric badge + backfill runs (2026-07-16)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the embedding-pipeline admin badge report-centric (`1456 / 4077 reports processed · 2000 queued · 500 in progress · 121 not queued`), with a per-backfill progress line backed by a new `EmbeddingBackfillRun` model (single active run enforced) and subjob mechanics demoted to a muted secondary line.

**Architecture:** Spec §6.8 in `docs/superpowers/specs/hybrid-search.md`. A slim run model records each backfill's baseline; `embed_reports_task` increments its counter after each successful subjob bulk-write (counter-based progress is immune to Procrastinate's delete-on-completion policy). `embed_pending` and the admin enqueue action create the run and refuse while one is active, auto-closing abandoned runs (zero live subjobs, unfinished). The badge template gains three tiers: global report fraction, active-run line with stall marker, muted subjob line.

**Tech Stack:** Django 5/6 ORM (`F()` counters, `Func`/`KeyTransform` JSONB aggregation), Procrastinate task args, Django admin template override, pytest-django transactional tests.

### Global Constraints (Current Plan)

- Line length 100 for Python (ruff), 120 for templates (djlint).
- Terminology per spec §6.8: keep `subjob` everywhere (`EMBEDDING_SUBJOB_SIZE`, `--subjob-size`, messages); the run is "the backfill".
- Single-active enforcement lives in `create_backfill_run()` only — no DB constraint ("active" involves queue state).
- `EmbeddingBackfillRun.total_reports >= 1` always: both entry points return early on an empty id list before creating a run (the `{% widthratio %}` in the template relies on this).
- Write-path (live-priority) enqueues never carry a `run_id`.
- **Test/migration workflow in this devcontainer:** the dev compose watch-syncs host→container one-way. Run `makemigrations` on the HOST (`FORCE_DEBUG_TOOLBAR=false uv run python manage.py makemigrations pgsearch` — no DB needed); run `migrate` and pytest INSIDE the web container. `docker exec` output capture is unreliable — redirect to a file and read it:
  `docker exec -e FORCE_DEBUG_TOOLBAR=false radis_dev-web-1 bash -c "uv run pytest <args> > /tmp/pytest.log 2>&1; echo exit=\$? >> /tmp/pytest.log"` then `docker exec radis_dev-web-1 cat /tmp/pytest.log` (retry the cat if empty).
- Pre-commit hooks run ruff/djlint on commit; if they modify files, re-add and re-commit.
- Commit message trailer: `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`.

### Task 1: `EmbeddingBackfillRun` model, migration, run-history admin

**Files:**
- Modify: `radis/pgsearch/models.py` (append model; add imports)
- Modify: `radis/pgsearch/admin.py` (register run listing)
- Create: `radis/pgsearch/migrations/0003_embeddingbackfillrun.py` (via makemigrations)
- Test: `radis/pgsearch/tests/test_backfill_run.py` (new)

**Interfaces:**
- Consumes: `procrastinate.contrib.django.models.ProcrastinateJob` (read-only ORM).
- Produces: `EmbeddingBackfillRun` with fields `started_at/finished_at/cancelled_at/total_reports/processed_reports/triggered_by`, property `is_active -> bool`, classmethod `get_active() -> EmbeddingBackfillRun | None`, method `live_subjob_count() -> int`. Tasks 2–4 rely on these exact names.

- [ ] **Step 1: Write the failing tests**

Create `radis/pgsearch/tests/test_backfill_run.py`:

```python
"""Tests for the EmbeddingBackfillRun model (spec §6.8)."""

import json

import pytest
from django.db import connection
from django.utils import timezone

from radis.pgsearch.models import EmbeddingBackfillRun

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture(autouse=True)
def _clear_procrastinate_jobs():
    """ProcrastinateJob is read-only via the ORM, so pytest-django's flush
    between transactional tests doesn't clear it. Truncate explicitly."""
    with connection.cursor() as cur:
        cur.execute("TRUNCATE procrastinate_jobs RESTART IDENTITY CASCADE")
    yield
    with connection.cursor() as cur:
        cur.execute("TRUNCATE procrastinate_jobs RESTART IDENTITY CASCADE")


def _insert_embed_job(status: str, run_id: int | None, report_ids: list[int] | None = None) -> None:
    args = {"report_ids": report_ids or []}
    if run_id is not None:
        args["run_id"] = run_id
    with connection.cursor() as cur:
        cur.execute(
            "INSERT INTO procrastinate_jobs "
            "(queue_name, task_name, priority, lock, queueing_lock, args, status, attempts) "
            "VALUES ('embeddings', 'radis.pgsearch.tasks.embed_reports_task', 0, NULL, NULL, "
            "%s, %s::procrastinate_job_status, 0)",
            [json.dumps(args), status],
        )


def test_is_active_semantics():
    run = EmbeddingBackfillRun.objects.create(total_reports=10, triggered_by="test")
    assert run.is_active
    run.finished_at = timezone.now()
    assert not run.is_active
    run.finished_at = None
    run.cancelled_at = timezone.now()
    assert not run.is_active


def test_get_active_returns_latest_active_or_none():
    assert EmbeddingBackfillRun.get_active() is None
    EmbeddingBackfillRun.objects.create(
        total_reports=5, triggered_by="old", finished_at=timezone.now()
    )
    active = EmbeddingBackfillRun.objects.create(total_reports=7, triggered_by="live")
    assert EmbeddingBackfillRun.get_active() == active


def test_live_subjob_count_scopes_to_run_and_live_statuses():
    run = EmbeddingBackfillRun.objects.create(total_reports=10, triggered_by="test")
    other = EmbeddingBackfillRun.objects.create(
        total_reports=3, triggered_by="other", cancelled_at=timezone.now()
    )
    _insert_embed_job("todo", run_id=run.pk, report_ids=[1, 2])
    _insert_embed_job("doing", run_id=run.pk, report_ids=[3])
    _insert_embed_job("failed", run_id=run.pk, report_ids=[4])   # not live
    _insert_embed_job("todo", run_id=other.pk, report_ids=[5])   # other run
    _insert_embed_job("todo", run_id=None, report_ids=[6])       # write-path job
    assert run.live_subjob_count() == 2
```

- [ ] **Step 2: Run tests to verify they fail**

Run (container, per Global Constraints): `uv run pytest radis/pgsearch/tests/test_backfill_run.py -q`
Expected: FAIL with `ImportError: cannot import name 'EmbeddingBackfillRun'`.

- [ ] **Step 3: Add the model**

Append to `radis/pgsearch/models.py` (add `from procrastinate.contrib.django.models import ProcrastinateJob` to the imports):

```python
class EmbeddingBackfillRun(models.Model):
    """One operator-triggered embedding backfill (`embed_pending` or the
    admin enqueue action). At most one run is active at a time, enforced by
    `tasks.create_backfill_run` rather than a DB constraint ("active"
    involves queue state). Write-path (live-priority) embedding work
    carries no run.

    Progress is counter-based: `embed_reports_task` increments
    `processed_reports` after each successful subjob bulk-write (immune to
    the worker's --delete-jobs policy) and stamps `finished_at` when the
    counter reaches `total_reports`. Failed subjobs never increment."""

    started_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    cancelled_at = models.DateTimeField(null=True, blank=True)
    total_reports = models.PositiveIntegerField()
    processed_reports = models.PositiveIntegerField(default=0)
    triggered_by = models.CharField(max_length=150)

    class Meta:
        ordering = ["-started_at"]

    def __str__(self) -> str:
        return f"Embedding backfill run {self.pk} ({self.processed_reports}/{self.total_reports})"

    @property
    def is_active(self) -> bool:
        return self.finished_at is None and self.cancelled_at is None

    @classmethod
    def get_active(cls) -> "EmbeddingBackfillRun | None":
        return (
            cls.objects.filter(finished_at__isnull=True, cancelled_at__isnull=True)
            .order_by("-started_at")
            .first()
        )

    def live_subjob_count(self) -> int:
        """Queued+running subjobs carrying this run's id. Zero while
        `processed < total` means the run is abandoned (dead worker or
        retry exhaustion) — see `tasks.create_backfill_run` and the
        badge's stall marker."""
        return ProcrastinateJob.objects.filter(
            task_name="radis.pgsearch.tasks.embed_reports_task",
            queue_name="embeddings",
            status__in=("todo", "doing"),
            args__run_id=self.pk,
        ).count()
```

- [ ] **Step 4: Generate the migration (HOST) and apply it (container)**

Host: `FORCE_DEBUG_TOOLBAR=false uv run python manage.py makemigrations pgsearch`
Expected: creates `radis/pgsearch/migrations/0003_embeddingbackfillrun.py`.
Container: `uv run ./manage.py migrate pgsearch` (via the docker-exec pattern).
Expected: `Applying pgsearch.0003_embeddingbackfillrun... OK`.

- [ ] **Step 5: Register the read-only run-history admin**

In `radis/pgsearch/admin.py`, import `EmbeddingBackfillRun` from `.models` and append:

```python
@admin.register(EmbeddingBackfillRun)
class EmbeddingBackfillRunAdmin(admin.ModelAdmin):
    """Read-only backfill history ("what did last night's backfill do?").
    Runs are created by embed_pending / the admin enqueue action and
    mutated only by the task counter and cancel — never by hand."""

    list_display = (
        "id",
        "started_at",
        "finished_at",
        "cancelled_at",
        "processed_reports",
        "total_reports",
        "triggered_by",
    )

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `uv run pytest radis/pgsearch/tests/test_backfill_run.py radis/pgsearch/tests/test_admin.py -q`
Expected: all PASS (test_admin.py is untouched but shares the DB — confirm no regression).

- [ ] **Step 7: Commit**

```bash
git add radis/pgsearch/models.py radis/pgsearch/admin.py radis/pgsearch/migrations/0003_embeddingbackfillrun.py radis/pgsearch/tests/test_backfill_run.py
git commit -m "Add EmbeddingBackfillRun model with run-history admin

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

### Task 2: Run plumbing — counter, finish flip, cancel stamp, single-active guard

**Files:**
- Modify: `radis/pgsearch/tasks.py`
- Test: `radis/pgsearch/tests/test_backfill_run.py` (guard tests), `radis/pgsearch/tests/test_embed_reports_task.py` (counter tests)

**Interfaces:**
- Consumes: `EmbeddingBackfillRun` (Task 1: `get_active()`, `live_subjob_count()`, fields).
- Produces (Task 3 relies on these): `class ActiveBackfillError(Exception)`; `create_backfill_run(total_reports: int, triggered_by: str) -> EmbeddingBackfillRun`; `enqueue_embed_reports(report_ids, *, subjob_size=None, priority=None, run_id: int | None = None) -> int`; `embed_reports_task(report_ids: list[int], run_id: int | None = None)`. `cancel_backfill_embeddings()` additionally stamps `cancelled_at` on active runs (return type unchanged: int).

- [ ] **Step 1: Write the failing tests**

Append to `radis/pgsearch/tests/test_backfill_run.py`:

```python
from radis.pgsearch.tasks import (
    ActiveBackfillError,
    cancel_backfill_embeddings,
    create_backfill_run,
)


def test_create_backfill_run_refuses_while_active_with_live_subjobs():
    active = EmbeddingBackfillRun.objects.create(total_reports=10, triggered_by="first")
    _insert_embed_job("todo", run_id=active.pk, report_ids=[1, 2])
    with pytest.raises(ActiveBackfillError, match="already active"):
        create_backfill_run(5, triggered_by="second")
    assert EmbeddingBackfillRun.objects.count() == 1


def test_create_backfill_run_auto_closes_abandoned_run():
    abandoned = EmbeddingBackfillRun.objects.create(total_reports=10, triggered_by="first")
    # no live jobs for `abandoned` -> it is auto-closed and superseded
    run = create_backfill_run(5, triggered_by="second")
    abandoned.refresh_from_db()
    assert abandoned.cancelled_at is not None
    assert run.is_active
    assert run.total_reports == 5
    assert EmbeddingBackfillRun.get_active() == run


def test_cancel_backfill_embeddings_stamps_active_runs():
    run = EmbeddingBackfillRun.objects.create(total_reports=10, triggered_by="test")
    cancel_backfill_embeddings()
    run.refresh_from_db()
    assert run.cancelled_at is not None
```

Append to `radis/pgsearch/tests/test_embed_reports_task.py` (reuse the file's existing mocking style and fixtures — e.g. its `no_retry_sleep` fixture and however it fakes `EmbeddingClient`; the snippets below assume a `_mock_embedding_client` patch that returns one `settings.EMBEDDING_DIM`-float vector per input text, matching the file's existing pattern):

```python
from radis.pgsearch.models import EmbeddingBackfillRun


def test_embed_reports_task_increments_run_counter_and_flips_finished():
    reports = [ReportFactory.create() for _ in range(3)]
    run = EmbeddingBackfillRun.objects.create(total_reports=3, triggered_by="test")
    with _mock_embedding_client():
        embed_reports_task([r.pk for r in reports], run_id=run.pk)
    run.refresh_from_db()
    assert run.processed_reports == 3
    assert run.finished_at is not None


def test_embed_reports_task_partial_progress_leaves_run_unfinished():
    reports = [ReportFactory.create() for _ in range(2)]
    run = EmbeddingBackfillRun.objects.create(total_reports=5, triggered_by="test")
    with _mock_embedding_client():
        embed_reports_task([r.pk for r in reports], run_id=run.pk)
    run.refresh_from_db()
    assert run.processed_reports == 2
    assert run.finished_at is None


def test_embed_reports_task_failure_does_not_increment_counter():
    reports = [ReportFactory.create() for _ in range(2)]
    run = EmbeddingBackfillRun.objects.create(total_reports=2, triggered_by="test")
    with _mock_embedding_client(error=EmbeddingClientError("boom")):
        with pytest.raises(EmbeddingClientError):
            embed_reports_task([r.pk for r in reports], run_id=run.pk)
    run.refresh_from_db()
    assert run.processed_reports == 0
    assert run.finished_at is None


def test_embed_reports_task_never_finishes_cancelled_run():
    reports = [ReportFactory.create() for _ in range(2)]
    run = EmbeddingBackfillRun.objects.create(
        total_reports=2, triggered_by="test", cancelled_at=timezone.now()
    )
    with _mock_embedding_client():
        embed_reports_task([r.pk for r in reports], run_id=run.pk)
    run.refresh_from_db()
    assert run.processed_reports == 2  # counter stays truthful
    assert run.finished_at is None     # but a cancelled run never "finishes"


def test_embed_reports_task_without_run_id_touches_no_run():
    reports = [ReportFactory.create() for _ in range(1)]
    run = EmbeddingBackfillRun.objects.create(total_reports=9, triggered_by="test")
    with _mock_embedding_client():
        embed_reports_task([r.pk for r in reports])
    run.refresh_from_db()
    assert run.processed_reports == 0
```

If `test_embed_reports_task.py` has no reusable client mock helper, add this one near its fixtures and use it in the tests above:

```python
from contextlib import contextmanager


@contextmanager
def _mock_embedding_client(error: Exception | None = None):
    """Patch EmbeddingClient so embed_documents returns one unit vector per
    text (or raises `error`)."""
    from django.conf import settings as dj_settings

    with patch("radis.pgsearch.tasks.EmbeddingClient") as mock_cls:
        instance = mock_cls.return_value.__enter__.return_value
        if error is not None:
            instance.embed_documents.side_effect = error
        else:
            instance.embed_documents.side_effect = lambda texts: [
                [0.1] * dj_settings.EMBEDDING_DIM for _ in texts
            ]
        yield instance
```

Also import `timezone` (`from django.utils import timezone`) in that file if not present.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest radis/pgsearch/tests/test_backfill_run.py radis/pgsearch/tests/test_embed_reports_task.py -q`
Expected: new tests FAIL (`ImportError: cannot import name 'ActiveBackfillError'`; `TypeError: embed_reports_task() got an unexpected keyword argument 'run_id'`). Existing tests PASS.

- [ ] **Step 3: Implement the plumbing in `radis/pgsearch/tasks.py`**

Add imports: `from django.db.models import F`, `from django.db.models.functions import Now`, `from django.utils import timezone`, and extend the models import to `from .models import EmbeddingBackfillRun, ReportSearchIndex`.

Add after `enqueue_bulk_index_reports`:

```python
class ActiveBackfillError(Exception):
    """Raised when starting a backfill while another is still active."""


def create_backfill_run(total_reports: int, triggered_by: str) -> EmbeddingBackfillRun:
    """Create the run row for a backfill, enforcing single-active (§6.8).

    Refuses while a run with live subjobs is active. An active run with NO
    live subjobs and an unfinished counter is abandoned (jobs lost to retry
    exhaustion or a dead worker): auto-close it and proceed, so a wedged
    run can never block future backfills. Small check-then-act race window
    is acceptable for operator tooling."""
    active = EmbeddingBackfillRun.get_active()
    if active is not None:
        if active.live_subjob_count() > 0:
            raise ActiveBackfillError(
                f"Backfill already active (run {active.pk}: "
                f"{active.processed_reports}/{active.total_reports} reports processed). "
                f"Cancel it first with `embed_cancel` or the admin button."
            )
        active.cancelled_at = timezone.now()
        active.save(update_fields=["cancelled_at"])
        logger.warning(
            "create_backfill_run: auto-closed abandoned run %d (%d/%d processed, "
            "no live subjobs)",
            active.pk,
            active.processed_reports,
            active.total_reports,
        )
    return EmbeddingBackfillRun.objects.create(
        total_reports=total_reports, triggered_by=triggered_by
    )
```

Extend `enqueue_embed_reports` — signature gains `run_id: int | None = None`; the defer call becomes:

```python
        kwargs: dict[str, JSONValue] = {"report_ids": list(chunk)}
        if run_id is not None:
            kwargs["run_id"] = run_id
        deferrer.defer(**kwargs)
```

(add one line to the docstring: `run_id` ties backfill subjobs to their `EmbeddingBackfillRun`; write-path enqueues leave it None.)

Extend `cancel_backfill_embeddings` — after the `cancelled = sum(...)` line, before the log:

```python
    closed_runs = EmbeddingBackfillRun.objects.filter(
        finished_at__isnull=True, cancelled_at__isnull=True
    ).update(cancelled_at=Now())
```

and extend the existing log line to include `closed_runs` (e.g. `"... cancelled %d of %d queued backfill subjob(s); closed %d run(s)"`).

Extend `embed_reports_task` — signature becomes `def embed_reports_task(report_ids: list[int], run_id: int | None = None) -> None:`; after the `bulk_update` block, before the duration log:

```python
    if run_id is not None and embedded:
        EmbeddingBackfillRun.objects.filter(pk=run_id).update(
            processed_reports=F("processed_reports") + len(embedded)
        )
        # Flip finished_at exactly once, and never on a cancelled run.
        EmbeddingBackfillRun.objects.filter(
            pk=run_id,
            finished_at__isnull=True,
            cancelled_at__isnull=True,
            processed_reports__gte=F("total_reports"),
        ).update(finished_at=Now())
```

(add to the task docstring: increments the backfill run's counter on success; failed subjobs never increment, so an abandoned run is detectable as `processed < total` with no live subjobs.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest radis/pgsearch/tests/test_backfill_run.py radis/pgsearch/tests/test_embed_reports_task.py -q`
Expected: all PASS, output pristine.

- [ ] **Step 5: Commit**

```bash
git add radis/pgsearch/tasks.py radis/pgsearch/tests/test_backfill_run.py radis/pgsearch/tests/test_embed_reports_task.py
git commit -m "Thread backfill runs through embed tasks with single-active guard

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

### Task 3: Entry points — `embed_pending` and admin enqueue action create runs

**Files:**
- Modify: `radis/pgsearch/management/commands/embed_pending.py`
- Modify: `radis/pgsearch/admin.py` (`enqueue_pending_embeddings` action)
- Test: `radis/pgsearch/tests/test_embed_pending_command.py`, `radis/pgsearch/tests/test_admin.py`

**Interfaces:**
- Consumes (Task 2): `create_backfill_run(total_reports, triggered_by) -> EmbeddingBackfillRun` raising `ActiveBackfillError`; `enqueue_embed_reports(..., run_id=...)`.
- Produces: `embed_pending` exits with `CommandError` when a backfill is active; admin action warns via `message_user` and enqueues nothing.

- [ ] **Step 1: Write the failing tests**

Append to `radis/pgsearch/tests/test_embed_pending_command.py` (follow the file's existing call style for invoking the command — `call_command("embed_pending", ...)` — and its existing fixtures; insert live jobs with the same raw-SQL pattern as `test_backfill_run.py`):

```python
from django.core.management import CommandError

from radis.pgsearch.models import EmbeddingBackfillRun


def test_embed_pending_creates_run_with_enqueued_total():
    [ReportFactory.create() for _ in range(3)]
    call_command("embed_pending")
    run = EmbeddingBackfillRun.objects.get()
    assert run.total_reports == 3
    assert run.is_active
    assert run.triggered_by == "embed_pending"


def test_embed_pending_refuses_while_backfill_active(insert_live_job_for_run):
    active = EmbeddingBackfillRun.objects.create(total_reports=10, triggered_by="first")
    insert_live_job_for_run(active)
    ReportFactory.create()
    with pytest.raises(CommandError, match="already active"):
        call_command("embed_pending")
    assert EmbeddingBackfillRun.objects.count() == 1


def test_embed_pending_supersedes_abandoned_run():
    EmbeddingBackfillRun.objects.create(total_reports=10, triggered_by="first")
    ReportFactory.create()
    call_command("embed_pending")
    assert EmbeddingBackfillRun.objects.filter(cancelled_at__isnull=False).count() == 1
    assert EmbeddingBackfillRun.get_active().triggered_by == "embed_pending"
```

(`insert_live_job_for_run` is a small fixture to add in that file wrapping the raw-SQL insert with `status="todo"` and `args={"report_ids": [1], "run_id": run.pk}` — copy the `_insert_embed_job` helper from `test_backfill_run.py`.)

Append to `radis/pgsearch/tests/test_admin.py`:

```python
def test_enqueue_pending_embeddings_creates_run_and_threads_run_id():
    targets = [ReportFactory.create() for _ in range(2)]
    selected = ReportSearchIndex.objects.filter(report_id__in=[r.pk for r in targets])
    admin_instance = ReportSearchIndexAdmin(ReportSearchIndex, AdminSite())
    admin_instance.message_user = MagicMock()
    request = MagicMock()
    request.user.get_username.return_value = "alice"

    admin_instance.enqueue_pending_embeddings(request, selected)

    run = EmbeddingBackfillRun.objects.get()
    assert run.total_reports == 2
    assert run.triggered_by == "alice"
    job_args = ProcrastinateJob.objects.filter(queue_name="embeddings").values_list(
        "args", flat=True
    )
    assert all(args.get("run_id") == run.pk for args in job_args)


def test_enqueue_pending_embeddings_warns_while_backfill_active():
    active = EmbeddingBackfillRun.objects.create(total_reports=10, triggered_by="first")
    # An args payload carrying the active run's id makes the guard see a live
    # subjob (the helper's default args carry no run_id, so it won't count):
    _insert_procrastinate_job(
        "todo", args_json=json.dumps({"report_ids": [1], "run_id": active.pk})
    )
    target = ReportFactory.create()
    selected = ReportSearchIndex.objects.filter(report_id=target.pk)
    admin_instance = ReportSearchIndexAdmin(ReportSearchIndex, AdminSite())
    admin_instance.message_user = MagicMock()
    request = MagicMock()
    request.user.get_username.return_value = "bob"

    admin_instance.enqueue_pending_embeddings(request, selected)

    assert EmbeddingBackfillRun.objects.count() == 1  # no new run
    call = admin_instance.message_user.call_args
    assert "already active" in call.args[1]
    assert call.kwargs.get("level") == messages.WARNING
```

(Imports to add in `test_admin.py`: `import json`, `from procrastinate.contrib.django.models import ProcrastinateJob`, `from django.contrib import messages`, `from radis.pgsearch.models import EmbeddingBackfillRun`.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest radis/pgsearch/tests/test_embed_pending_command.py radis/pgsearch/tests/test_admin.py -q`
Expected: new tests FAIL (no run rows created; no warning message). Existing tests PASS.

- [ ] **Step 3: Wire `embed_pending`**

In `radis/pgsearch/management/commands/embed_pending.py`: import `CommandError` (`from django.core.management.base import BaseCommand, CommandError`) and `create_backfill_run`, `ActiveBackfillError` from `radis.pgsearch.tasks`. In `handle()`, after the `if not ids: ... return` guard and before `enqueue_embed_reports`:

```python
        try:
            run = create_backfill_run(len(ids), triggered_by="embed_pending")
        except ActiveBackfillError as exc:
            raise CommandError(str(exc)) from exc
```

and pass `run_id=run.pk` to the existing `enqueue_embed_reports(...)` call. Extend the final `self.stdout.write` success line to include the run id (e.g. `f"Done. Deferred {subjob_count} subjob(s) for run {run.pk}."`) and the module docstring's operator notes with one line: only one backfill can be active at a time; a wedged (abandoned) run is auto-superseded.

- [ ] **Step 4: Wire the admin action**

In `radis/pgsearch/admin.py` `enqueue_pending_embeddings`, import `ActiveBackfillError, create_backfill_run` alongside the existing `.tasks` imports. After the `if not report_ids:` guard, before `enqueue_embed_reports`:

```python
        try:
            run = create_backfill_run(
                len(report_ids), triggered_by=request.user.get_username()
            )
        except ActiveBackfillError as exc:
            self.message_user(request, str(exc), level=messages.WARNING)
            return
```

and pass `run_id=run.pk` to `enqueue_embed_reports(...)`. Include the run id in the success `message_user` text and the audit log line.

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest radis/pgsearch/tests/test_embed_pending_command.py radis/pgsearch/tests/test_admin.py radis/pgsearch/tests/test_backfill_run.py -q`
Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add radis/pgsearch/management/commands/embed_pending.py radis/pgsearch/admin.py radis/pgsearch/tests/test_embed_pending_command.py radis/pgsearch/tests/test_admin.py
git commit -m "Create backfill runs from embed_pending and admin enqueue action

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

### Task 4: Report-centric badge — stats keys and three-tier template

**Files:**
- Modify: `radis/pgsearch/admin.py` (`_embedding_pipeline_stats`)
- Modify: `radis/pgsearch/templates/admin/pgsearch/reportsearchindex/change_list.html`
- Test: `radis/pgsearch/tests/test_admin.py`

**Interfaces:**
- Consumes (Tasks 1–2): `EmbeddingBackfillRun.get_active()`, `.live_subjob_count()`.
- Produces: `_embedding_pipeline_stats() -> dict[str, Any]` with keys `total_reports, embedded_reports, pending_reports, todo, todo_reports, todo_backfill, doing, doing_reports, unqueued_reports, failed` (ints) plus `run` (`EmbeddingBackfillRun | None`) and `run_stalled` (bool). Badge HTML per spec §6.8.

- [ ] **Step 1: Update/replace the stats and rendering tests**

In `radis/pgsearch/tests/test_admin.py`:

Replace `test_pipeline_stats_zero_when_no_queue_activity` with:

```python
def test_pipeline_stats_zero_when_no_queue_activity():
    stats = ReportSearchIndexAdmin._embedding_pipeline_stats()
    assert stats == {
        "total_reports": 0,
        "embedded_reports": 0,
        "pending_reports": 0,
        "todo": 0,
        "todo_reports": 0,
        "todo_backfill": 0,
        "doing": 0,
        "doing_reports": 0,
        "unqueued_reports": 0,
        "failed": 0,
        "run": None,
        "run_stalled": False,
    }
```

Add:

```python
def test_pipeline_stats_report_centric_keys():
    pending = [ReportFactory.create() for _ in range(4)]
    embedded = ReportFactory.create()
    rsi = ReportSearchIndex.objects.get(report_id=embedded.pk)
    rsi.embedding = [0.0] * 1024
    rsi.save()
    _insert_procrastinate_job("todo", report_ids=[pending[0].pk, pending[1].pk])
    _insert_procrastinate_job("doing", report_ids=[pending[2].pk])

    stats = ReportSearchIndexAdmin._embedding_pipeline_stats()
    assert stats["total_reports"] == 5
    assert stats["embedded_reports"] == 1
    assert stats["pending_reports"] == 4
    assert stats["unqueued_reports"] == 1  # 4 pending - 2 queued - 1 doing


def test_pipeline_stats_unqueued_clamped_at_zero():
    [ReportFactory.create() for _ in range(1)]
    _insert_procrastinate_job("todo", report_ids=[1, 2, 3])  # covers more than pending
    stats = ReportSearchIndexAdmin._embedding_pipeline_stats()
    assert stats["unqueued_reports"] == 0


def test_pipeline_stats_active_run_and_stall_flag():
    run = EmbeddingBackfillRun.objects.create(total_reports=10, triggered_by="t")
    stats = ReportSearchIndexAdmin._embedding_pipeline_stats()
    assert stats["run"] == run
    assert stats["run_stalled"] is True  # unfinished, no live subjobs

    import json as _json
    _insert_procrastinate_job(
        "todo", args_json=_json.dumps({"report_ids": [1], "run_id": run.pk})
    )
    stats = ReportSearchIndexAdmin._embedding_pipeline_stats()
    assert stats["run_stalled"] is False
```

Replace the two Task-2-era rendering tests (`test_changelist_badge_shows_subjob_report_counts`, `test_changelist_badge_zero_counts_render_plain`) with:

```python
def test_changelist_badge_report_centric_primary_line(admin_client):
    pending = [ReportFactory.create() for _ in range(3)]
    done = ReportFactory.create()
    rsi = ReportSearchIndex.objects.get(report_id=done.pk)
    rsi.embedding = [0.0] * 1024
    rsi.save()
    _insert_procrastinate_job("todo", report_ids=[pending[0].pk, pending[1].pk])
    _insert_procrastinate_job("doing", report_ids=[pending[2].pk])

    url = reverse("admin:pgsearch_reportsearchindex_changelist")
    html = _squash_ws(admin_client.get(url).content.decode())

    assert "<strong>1</strong> / <strong>4</strong> reports processed" in html
    assert "2 queued" in html
    assert "1 in progress" in html
    assert "not queued" not in html  # zero segment omitted
    assert "subjobs: 1 queued · 1 in-flight · 0 failed" in html


def test_changelist_badge_idle_state_is_bare_fraction(admin_client):
    done = ReportFactory.create()
    rsi = ReportSearchIndex.objects.get(report_id=done.pk)
    rsi.embedding = [0.0] * 1024
    rsi.save()

    url = reverse("admin:pgsearch_reportsearchindex_changelist")
    html = _squash_ws(admin_client.get(url).content.decode())

    assert "<strong>1</strong> / <strong>1</strong> reports processed" in html
    assert "queued" not in html
    assert "in progress" not in html
    assert "subjobs:" not in html
    assert "Backfill:" not in html


def test_changelist_badge_backfill_line_with_stall_marker(admin_client):
    EmbeddingBackfillRun.objects.create(
        total_reports=8, processed_reports=2, triggered_by="t"
    )
    url = reverse("admin:pgsearch_reportsearchindex_changelist")
    html = _squash_ws(admin_client.get(url).content.decode())

    assert "Backfill: <strong>2</strong> / <strong>8</strong> reports processed (25%)" in html
    assert "stalled — no live subjobs" in html
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest radis/pgsearch/tests/test_admin.py -q`
Expected: the new/replaced tests FAIL (missing keys; old badge markup). Untouched tests PASS.

- [ ] **Step 3: Rewrite `_embedding_pipeline_stats`**

In `radis/pgsearch/admin.py` (typing import: `from typing import Any`):

```python
    @staticmethod
    def _embedding_pipeline_stats() -> dict[str, Any]:
        """Snapshot for the admin badge (spec §6.8), report-centric:
        the global processed fraction plus a breakdown of the remainder
        (queued / in progress / not queued), the active backfill run with
        a stall flag, and the subjob mechanics for the secondary line.
        Report totals per status are summed DB-side from each job's
        args->'report_ids' (the id arrays never leave Postgres)."""
        total = ReportSearchIndex.objects.count()
        pending = ReportSearchIndex.objects.filter(embedding__isnull=True).count()
        report_count = Func(
            KeyTransform("report_ids", "args"),
            function="jsonb_array_length",
            output_field=IntegerField(),
        )
        queue_rows = {
            row["status"]: row
            for row in ProcrastinateJob.objects.filter(queue_name="embeddings")
            .values("status")
            .annotate(jobs=Count("id"), reports=Sum(report_count))
        }
        # Counted separately because the cancel-backfill button only cancels
        # backfill-priority jobs — gating it on the overall todo count would
        # offer a cancel that then reports "nothing to cancel" whenever the
        # queue holds only live write-path jobs.
        todo_backfill = ProcrastinateJob.objects.filter(
            queue_name="embeddings",
            status="todo",
            priority=settings.EMBEDDING_BACKFILL_PRIORITY,
        ).count()
        todo_row = queue_rows.get("todo", {})
        doing_row = queue_rows.get("doing", {})
        todo_reports = todo_row.get("reports") or 0
        doing_reports = doing_row.get("reports") or 0
        run = EmbeddingBackfillRun.get_active()
        run_stalled = bool(
            run
            and run.processed_reports < run.total_reports
            and run.live_subjob_count() == 0
        )
        return {
            "total_reports": total,
            "embedded_reports": total - pending,
            "pending_reports": pending,
            "todo": todo_row.get("jobs", 0),
            "todo_reports": todo_reports,
            "todo_backfill": todo_backfill,
            "doing": doing_row.get("jobs", 0),
            "doing_reports": doing_reports,
            # Clamped: the counts above aren't one snapshot.
            "unqueued_reports": max(0, pending - todo_reports - doing_reports),
            "failed": queue_rows.get("failed", {}).get("jobs", 0),
            "run": run,
            "run_stalled": run_stalled,
        }
```

- [ ] **Step 4: Rewrite the badge template**

Replace the badge `<div class="module">...</div>` content in `radis/pgsearch/templates/admin/pgsearch/reportsearchindex/change_list.html` with (keep the surrounding `{% extends %}`/`{% block %}`/`{% if embedding_pipeline_stats %}` frame and the outer div's style):

```django
            <strong>Embedding pipeline</strong>
            &nbsp;·&nbsp;
            <strong>{{ embedding_pipeline_stats.embedded_reports }}</strong> /
            <strong>{{ embedding_pipeline_stats.total_reports }}</strong> reports processed
            {% if embedding_pipeline_stats.pending_reports %}
                {% if embedding_pipeline_stats.todo_reports %}
                    &nbsp;·&nbsp; {{ embedding_pipeline_stats.todo_reports }} queued
                {% endif %}
                {% if embedding_pipeline_stats.doing_reports %}
                    &nbsp;·&nbsp; {{ embedding_pipeline_stats.doing_reports }} in progress
                {% endif %}
                {% if embedding_pipeline_stats.unqueued_reports %}
                    &nbsp;·&nbsp; {{ embedding_pipeline_stats.unqueued_reports }} not queued
                {% endif %}
            {% endif %}
            {% if embedding_pipeline_stats.run %}
                <div style="margin-top: 4px;">
                    Backfill: <strong>{{ embedding_pipeline_stats.run.processed_reports }}</strong> /
                    <strong>{{ embedding_pipeline_stats.run.total_reports }}</strong> reports processed
                    ({% widthratio embedding_pipeline_stats.run.processed_reports embedding_pipeline_stats.run.total_reports 100 %}%)
                    · started {{ embedding_pipeline_stats.run.started_at|timesince }} ago
                    {% if embedding_pipeline_stats.run_stalled %}
                        <strong style="color: #ba2121;">· stalled — no live subjobs</strong>
                    {% endif %}
                </div>
            {% endif %}
            {% if embedding_pipeline_stats.todo or embedding_pipeline_stats.doing or embedding_pipeline_stats.failed %}
                <div style="margin-top: 4px; color: #666;">
                    subjobs: {{ embedding_pipeline_stats.todo }} queued
                    · {{ embedding_pipeline_stats.doing }} in-flight
                    ·
                    {% if embedding_pipeline_stats.failed %}
                        <strong style="color: #ba2121;">{{ embedding_pipeline_stats.failed }}</strong>
                    {% else %}
                        0
                    {% endif %}
                    failed
                    <span>(<code>embeddings</code> queue)</span>
                    {% if embedding_pipeline_stats.todo_backfill %}
                        <form method="post"
                              action="{% url 'admin:pgsearch_reportsearchindex_cancel_backfill' %}"
                              style="display: inline;
                                     margin-left: 10px">
                            {% csrf_token %}
                            <button type="submit" class="button">Cancel queued backfill</button>
                        </form>
                    {% endif %}
                </div>
            {% endif %}
```

- [ ] **Step 5: Run tests, then lint**

Run: `uv run pytest radis/pgsearch/tests/test_admin.py -q` — Expected: all PASS.
Run: `uv run cli lint` — Expected: clean; if djlint reformats the template, re-run the badge tests (the `_squash_ws` helper tolerates rewrapping) and include the reformatted file.

- [ ] **Step 6: Commit**

```bash
git add radis/pgsearch/admin.py radis/pgsearch/templates/admin/pgsearch/reportsearchindex/change_list.html radis/pgsearch/tests/test_admin.py
git commit -m "Report-centric embedding pipeline badge with backfill run line

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

### Task 5: Sync the living spec

**Files:**
- Modify: `docs/superpowers/specs/hybrid-search.md` (header only)

**Interfaces:**
- Consumes: nothing. Produces: spec header no longer marks §6.8 as pending.

- [ ] **Step 1: Update the status header**

Change the `**Status:**` line to: `**Status:** Implemented on `feat/hybrid-search` — living document, last synced to code <today's date>.` (drop the §6.8 exception clause).

- [ ] **Step 2: Commit**

```bash
git add docs/superpowers/specs/hybrid-search.md
git commit -m "Mark spec §6.8 as implemented

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```
