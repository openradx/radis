# Backfill Cancel + Throughput Knobs Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let operators cancel a queued embedding backfill, and make the throughput knobs (batch size, subjob size, request timeout, worker concurrency) env-configurable with gentler defaults, per `docs/superpowers/specs/2026-07-02-backfill-cancel-and-throughput-design.md`.

**Architecture:** Backfill subjobs are already distinguishable (priority 0 vs live's 1), so cancellation is a filtered sweep over Procrastinate's own job table using its race-safe `cancel_job_by_id`. Throughput knobs move from hardcoded constants to `env.int` reads; the compose worker concurrency becomes an interpolated env var.

**Tech Stack:** Procrastinate 3.9 (`ProcrastinateJob` read-only model + `app.job_manager`), Django management commands, environs.

## Global Constraints

- Line length 100 (Ruff); tests run `POSTGRES_DEV_PORT=<port> uv run pytest ...` against a throwaway `pgvector/pgvector:pg17` container (localhost:5432 lacks pgvector).
- Task name string is exactly `"radis.pgsearch.tasks.embed_reports_task"`, queue `"embeddings"`.
- Never touch priority-`EMBEDDING_LIVE_PRIORITY` jobs.
- Commits end with `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`.

---

### Task 1: `cancel_backfill_embeddings` helper

**Files:**
- Modify: `radis/pgsearch/tasks.py` (imports + new function after `enqueue_embed_reports`)
- Test: `radis/pgsearch/tests/test_embed_reports_task.py` (append)

**Interfaces:**
- Produces: `radis.pgsearch.tasks.cancel_backfill_embeddings() -> int` — cancels queued backfill subjobs, returns count actually cancelled.

- [ ] **Step 1: Write the failing test** (append to `radis/pgsearch/tests/test_embed_reports_task.py`):

```python
def test_cancel_backfill_embeddings_cancels_only_queued_backfill_jobs(settings):
    from procrastinate.contrib.django.models import ProcrastinateJob

    from radis.pgsearch import tasks as tasks_module

    tasks_module.enqueue_embed_reports(
        [1, 2, 3], subjob_size=1, priority=settings.EMBEDDING_BACKFILL_PRIORITY
    )
    tasks_module.enqueue_embed_reports([4], priority=settings.EMBEDDING_LIVE_PRIORITY)

    cancelled = tasks_module.cancel_backfill_embeddings()

    assert cancelled == 3
    by_priority = {
        priority: status
        for priority, status in ProcrastinateJob.objects.filter(
            task_name="radis.pgsearch.tasks.embed_reports_task"
        ).values_list("priority", "status")
    }
    assert by_priority[settings.EMBEDDING_BACKFILL_PRIORITY] == "cancelled"
    assert by_priority[settings.EMBEDDING_LIVE_PRIORITY] == "todo"


def test_cancel_backfill_embeddings_returns_zero_when_queue_empty():
    from radis.pgsearch import tasks as tasks_module

    assert tasks_module.cancel_backfill_embeddings() == 0
```

(The file's module-level `pytestmark = pytest.mark.django_db(transaction=True)` already covers DB access.)

- [ ] **Step 2: Run to verify failure**

Run: `POSTGRES_DEV_PORT=54329 uv run pytest radis/pgsearch/tests/test_embed_reports_task.py -k cancel_backfill -v`
Expected: FAIL, `AttributeError: ... no attribute 'cancel_backfill_embeddings'`

- [ ] **Step 3: Implement** — in `radis/pgsearch/tasks.py`, add to the imports block `from procrastinate.contrib.django.models import ProcrastinateJob`, then after `enqueue_embed_reports`:

```python
def cancel_backfill_embeddings() -> int:
    """Cancel every queued (todo) backfill-priority embed subjob.

    "The backfill" has no job object of its own — it is exactly the
    embed_reports_task jobs enqueued at EMBEDDING_BACKFILL_PRIORITY
    (embed_pending / admin action), which the live write-path priority
    keeps distinct. Cancellation goes job-by-job through Procrastinate's
    cancel_job_by_id, which is race-safe: a job a worker grabbed between
    our select and the cancel returns False and simply runs to completion.
    Returns the number of jobs actually cancelled. Resume = re-run
    embed_pending (idempotent, embedding IS NULL filter)."""
    job_ids = list(
        ProcrastinateJob.objects.filter(
            task_name="radis.pgsearch.tasks.embed_reports_task",
            queue_name="embeddings",
            status="todo",
            priority=settings.EMBEDDING_BACKFILL_PRIORITY,
        ).values_list("id", flat=True)
    )
    cancelled = sum(1 for job_id in job_ids if app.job_manager.cancel_job_by_id(job_id))
    logger.info(
        "cancel_backfill_embeddings: cancelled %d of %d queued backfill subjob(s)",
        cancelled,
        len(job_ids),
    )
    return cancelled
```

- [ ] **Step 4: Run to verify pass**

Run: `POSTGRES_DEV_PORT=54329 uv run pytest radis/pgsearch/tests/test_embed_reports_task.py -k cancel_backfill -v`
Expected: 2 PASS

- [ ] **Step 5: Commit**

```bash
git add radis/pgsearch/tasks.py radis/pgsearch/tests/test_embed_reports_task.py
git commit -m "feat(pgsearch): cancel_backfill_embeddings helper

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 2: `embed_cancel` management command

**Files:**
- Create: `radis/pgsearch/management/commands/embed_cancel.py`
- Test: `radis/pgsearch/tests/test_embed_reports_task.py` (append)

**Interfaces:**
- Consumes: `cancel_backfill_embeddings()` from Task 1.
- Produces: `./manage.py embed_cancel`.

- [ ] **Step 1: Write the failing test** (append):

```python
def test_embed_cancel_command_reports_count(settings):
    from io import StringIO

    from django.core.management import call_command

    from radis.pgsearch import tasks as tasks_module

    tasks_module.enqueue_embed_reports(
        [1, 2], subjob_size=1, priority=settings.EMBEDDING_BACKFILL_PRIORITY
    )

    out = StringIO()
    call_command("embed_cancel", stdout=out)

    assert "Cancelled 2 queued backfill subjob(s)" in out.getvalue()


def test_embed_cancel_command_handles_empty_queue():
    from io import StringIO

    from django.core.management import call_command

    out = StringIO()
    call_command("embed_cancel", stdout=out)

    assert "No queued backfill subjobs to cancel." in out.getvalue()
```

- [ ] **Step 2: Run to verify failure**

Run: `POSTGRES_DEV_PORT=54329 uv run pytest radis/pgsearch/tests/test_embed_reports_task.py -k embed_cancel -v`
Expected: FAIL, `CommandError: Unknown command: 'embed_cancel'`

- [ ] **Step 3: Implement** `radis/pgsearch/management/commands/embed_cancel.py`:

```python
"""Cancel a running embedding backfill.

Counterpart to `embed_pending`: cancels every embed_reports_task subjob
still queued at backfill priority. Subjobs already being executed (at most
the embeddings worker's --concurrency) finish their current chunk; live
write-path embedding subjobs are untouched. Re-running `embed_pending`
later resumes exactly where things stopped — its `embedding IS NULL`
filter makes it idempotent.
"""

import logging

from django.core.management.base import BaseCommand

from radis.pgsearch.tasks import cancel_backfill_embeddings

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = (
        "Cancel every queued embedding-backfill subjob (todo jobs at "
        "backfill priority). Running subjobs finish their current chunk; "
        "live write-path embedding is untouched."
    )

    def handle(self, *args, **opts) -> None:
        cancelled = cancel_backfill_embeddings()
        if cancelled == 0:
            self.stdout.write("No queued backfill subjobs to cancel.")
            return
        self.stdout.write(
            self.style.SUCCESS(f"Cancelled {cancelled} queued backfill subjob(s).")
        )
        self.stdout.write(
            "Running subjobs (at most the worker's concurrency) will finish "
            "their current chunk. Re-run `./manage.py embed_pending` to resume."
        )
```

- [ ] **Step 4: Run to verify pass**

Run: `POSTGRES_DEV_PORT=54329 uv run pytest radis/pgsearch/tests/test_embed_reports_task.py -k embed_cancel -v`
Expected: 2 PASS

- [ ] **Step 5: Commit**

```bash
git add radis/pgsearch/management/commands/embed_cancel.py radis/pgsearch/tests/test_embed_reports_task.py
git commit -m "feat(pgsearch): embed_cancel command to stop a queued backfill

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 3: env-configurable throughput knobs

**Files:**
- Modify: `radis/settings/base.py:346-351`
- Modify: `docker-compose.prod.yml:85` (embeddings worker command)
- Modify: `example.env` (embedding section)
- Modify: `radis/pgsearch/tasks.py` (`enqueue_embed_reports` docstring line about "both currently default to 1000")

**Interfaces:**
- Produces: `EMBEDDING_BATCH_SIZE` (default 200), `EMBEDDING_SUBJOB_SIZE` (default 1000), `EMBEDDING_REQUEST_TIMEOUT` (default 30) as env-backed settings; `EMBEDDINGS_WORKER_CONCURRENCY` compose variable (default 2).

- [ ] **Step 1: Settings** — in `radis/settings/base.py` replace:

```python
EMBEDDING_REQUEST_TIMEOUT = 30
```
```python
EMBEDDING_BATCH_SIZE = 1000
EMBEDDING_SUBJOB_SIZE = 1000
```

with:

```python
EMBEDDING_REQUEST_TIMEOUT = env.int("EMBEDDING_REQUEST_TIMEOUT", default=30)
```
```python
# Texts per HTTP call. A 429'd or timed-out call wastes its whole payload
# and retries every text in it, so smaller batches bound the waste and
# consume the gateway's sliding window in smoother increments.
EMBEDDING_BATCH_SIZE = env.int("EMBEDDING_BATCH_SIZE", default=200)
# Reports per Procrastinate subjob (task granularity, not HTTP granularity).
EMBEDDING_SUBJOB_SIZE = env.int("EMBEDDING_SUBJOB_SIZE", default=1000)
```

- [ ] **Step 2: Compose** — in `docker-compose.prod.yml`, embeddings worker command: change `--concurrency 4` to `--concurrency ${EMBEDDINGS_WORKER_CONCURRENCY:-2}`. Also add to `docker-compose.base.yml`'s x-app environment block (so dev compose can pass the settings through):

```yaml
    EMBEDDING_BATCH_SIZE: ${EMBEDDING_BATCH_SIZE:-200}
    EMBEDDING_SUBJOB_SIZE: ${EMBEDDING_SUBJOB_SIZE:-1000}
    EMBEDDING_REQUEST_TIMEOUT: ${EMBEDDING_REQUEST_TIMEOUT:-30}
```

Literal defaults are required (not `:-` empty): `env.int` raises on an
empty-string value, and the container always receives the variable once
it's listed in the environment block.

CAUTION: `docker-compose.base.yml` has uncommitted user changes (CA-cert
mounts) in the working tree. Protect them: `git stash push
docker-compose.base.yml`, apply this edit, commit, then `git stash pop`
(the hunks touch different regions and re-apply cleanly). STOP and report
if the pop conflicts.

- [ ] **Step 3: example.env** — append to the embedding section:

```bash
# Embedding throughput tuning (all optional).
# Texts per HTTP call to the embedding service. Smaller = less wasted work
# per 429/timeout, smoother rate-limit consumption; larger = fewer calls.
#EMBEDDING_BATCH_SIZE=200
# Reports per background subjob (Procrastinate task granularity).
#EMBEDDING_SUBJOB_SIZE=1000
# HTTP timeout in seconds for one embedding call.
#EMBEDDING_REQUEST_TIMEOUT=30
# Concurrent subjobs the embeddings worker executes (compose interpolation).
#EMBEDDINGS_WORKER_CONCURRENCY=2
```

- [ ] **Step 4: Fix the stale docstring** in `radis/pgsearch/tasks.py` `enqueue_embed_reports`: replace the sentence "though both currently default to 1000 — one HTTP call per subjob." with "each subjob makes ceil(subjob_size / EMBEDDING_BATCH_SIZE) HTTP calls."

- [ ] **Step 5: Verify**

Run: `POSTGRES_DEV_PORT=54329 uv run pytest radis/pgsearch/ 2>&1 | tail -2` — expect all pass.
Run: `docker compose -f docker-compose.base.yml -f docker-compose.prod.yml config --quiet && echo OK` — expect OK.
Run: `uv run cli lint` — expect clean.

- [ ] **Step 6: Commit**

```bash
git add radis/settings/base.py docker-compose.prod.yml example.env radis/pgsearch/tasks.py docker-compose.base.yml
git commit -m "feat(pgsearch): env-configurable embedding throughput knobs, gentler defaults

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```
