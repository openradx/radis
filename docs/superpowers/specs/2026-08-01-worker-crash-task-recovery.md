# Recovering tasks left at IN_PROGRESS when a worker dies

## Context

### The bug

When a worker process is killed (SIGKILL, OOM, machine down), the `finally` block in
`AnalysisTaskProcessor.start()` (`radis/core/processors.py:69-73`) never runs. Nothing is written,
so no row is corrected: the task stays `IN_PROGRESS`, its Procrastinate row stays `doing`, and the
only thing that changes is that the worker stops writing its heartbeat.

`retry_stalled_jobs` (in `adit-radis-shared`, periodic every 10 min plus once per deploy) later
re-queues that row, a worker picks it up, and `radis/core/processors.py:37` asserts:

```python
assert task.status == task.Status.PENDING
```

The task is `IN_PROGRESS`, so the assert raises. Procrastinate marks the run failed and — because
`bg_worker` defaults to `--delete-jobs=always` — **deletes the queue row**. The task is now
`IN_PROGRESS` with no queue row: nothing will ever hand it to a worker again.

If the user then cancels, `cancel_job()` (`radis/core/utils/model_utils.py:22`) parks the job at
`CANCELING`, where `update_job_state()` keeps it forever because a non-terminal task remains. For
`radis.labels` this also blocks every future labeling job, since `CANCELING` counts as active in the
`one_active_labeling_job` singleton index. `KNOWLEDGE.md:55-72` documents the manual shell recovery.

### How death is detected

A task is described by rows in three tables, chained by two foreign keys:

| table                   | owner         | relevant columns                         |
| ----------------------- | ------------- | ---------------------------------------- |
| `radis_*_*task`         | ours          | `status`, `queued_job_id`                |
| `procrastinate_jobs`    | Procrastinate | `status` (`todo`/`doing`/…), `worker_id` |
| `procrastinate_workers` | Procrastinate | `last_heartbeat`                         |

A live worker writes `last_heartbeat` every 10 s from its own async loop
(`procrastinate/worker.py:48`), independently of what the current task is doing — a slow LLM batch
never makes a worker look dead.

Procrastinate already defines death as **30 s of heartbeat silence**: `get_stalled_jobs()` defaults
to `seconds_since_heartbeat=30` (`procrastinate/manager.py:207`) and the shared
`retry_stalled_jobs` command calls it with no arguments. Worker rows are pruned at the same 30 s
threshold (`procrastinate/worker.py:49`), but only once per worker boot
(`procrastinate/worker.py:377-380`) — so a dead worker's row often persists, and detection must not
depend on it disappearing.

We adopt the same 30 s. It must never be shorter (we would rewrite state while Procrastinate still
considers the worker alive); making it longer only delays repair, since the platform acts at 30 s
regardless.

## Design

One repair rule, applied at two sweep entry points. Repair authority lives only in the sweep; the
processor claims and executes work, but never repairs.

### The rule

Given a task with `status == IN_PROGRESS`:

1. **Is the worker that was running it gone?** Yes if any of:
   - `queued_job_id IS NULL` (row deleted), or
   - the queue row's status is terminal (`succeeded`, `failed`, `cancelled`, `aborted`), or
   - the queue row's status is `todo` (only reachable post-crash, via `retry_stalled_jobs`), or
   - the queue row is `doing` **and** (`worker_id IS NULL` **or** `last_heartbeat` older than
     `ANALYSIS_STALLED_WORKER_GRACE_SECONDS`).

   If none hold — a `doing` row with a live worker — **leave the task untouched**.

2. **Is the job being cancelled** (`CANCELING` or `CANCELED`)? Then `task.status = CANCELED`.
   This branch needs no proof the old process stopped: the user asked for the work to end.

3. **Otherwise** `task.status = PENDING`, so the task is simply run again.

Then call `job.update_job_state()`, which drains a `CANCELING` job to `CANCELED`.

### Why PENDING and not FAILURE

Resuming costs the user nothing but time, and for labels it is nearly free: `label_report()`
(`radis/labels/labeling.py`) skips reports that already have fresh results and writes via
`update_or_create`, so a resumed batch only pays for the reports it had not finished.

`IN_PROGRESS` must keep meaning "a worker is on this right now". The sweep therefore normalises a
stale task back to `PENDING`, and the processor only ever enters execution from `PENDING` — rather
than widening the guard to accept `IN_PROGRESS` as ADIT does (`adit/core/tasks.py:72`), which makes
the status mean two things at once.

### Entry point A — startup sweep

A management command run inside each worker container before `bg_worker`. It is the only thing that
can act while no worker exists to pull from the queue: after a server crash, after a long outage,
and for tasks whose queue row is gone entirely (nothing can ever fire for those).

The sweep is **not queue-scoped** — it reads our task tables directly and scans every concrete
`AnalysisTask` subclass. This is deliberate: a `default_worker` restart can then repair a labeling
task whose own `llm_worker` is dead and not coming back.

### Entry point B — the periodic sweep

After a supervised restart, the startup sweep runs too early to help: Swarm restarts within
seconds, so the dead worker's heartbeat is only seconds old and the sweep correctly declines. The
startup sweep gets one look per container boot, so it never sees the task again.

The same `sweep_stale_analysis_state()` therefore also runs periodically, as a `@app.periodic`
Procrastinate task in a new `radis/core/tasks.py`, mirroring `retry_stalled_jobs` in
`adit-radis-shared`:

```python
@app.periodic(cron=settings.ANALYSIS_SWEEP_CRON)
@app.task(queueing_lock="sweep_stale_tasks")
def sweep_stale_tasks_periodic(timestamp: int):
    sweep_stale_analysis_state()
```

Default cadence is every minute. The sweep is one cheap indexed query per task model, normally
matching zero rows, and the fast cadence means it usually repairs a crashed task *before*
`retry_stalled_jobs` (every 10 min) re-queues its row — the row then fires into a `PENDING` task
and runs immediately, so worst-case recovery stays near the `retry_stalled_jobs` cadence.

Unlike the startup command (which must always exit 0 so the worker boots), the periodic task may
raise freely: a failed tick just logs, and the `queueing_lock` prevents pileup.

Known limitation: the periodic task runs on the `default` queue. If `default_worker` itself is
dead and stays dead, ticks stop and repair waits for the next container boot (startup sweep) —
part of why both entry points are kept.

The processor repairs nothing. It replaces today's assert with an atomic claim (change 5): it
executes a task only if it can flip it `PENDING → IN_PROGRESS` in a single conditional `UPDATE`,
and otherwise consumes its queue row as a logged no-op. A stale `IN_PROGRESS` task left by a
killed worker is deliberately left untouched there; the next sweep tick finds it as an orphan
(`queued_job` NULL after `--delete-jobs=always`) and repairs and re-queues it.

### Why every ordering converges

The sweep and the processor run concurrently, so neither may decide from a read made earlier —
both write task status only through atomic conditional `UPDATE`s:

- **The processor's claim** makes "only PENDING tasks execute" a database-enforced rule. A queue
  row can only be consumed without running when the task truly was not `PENDING` at that instant —
  leaving it either terminal (nothing to do) or stale `IN_PROGRESS` (an orphan the next sweep tick
  repairs). A `PENDING` task can never have its row wasted, because a claim against `PENDING`
  succeeds and runs.
- **The sweep's resolve UPDATE re-checks the owner-gone conditions in its WHERE clause**, not just
  `status=IN_PROGRESS`, so a late UPDATE cannot flip a task a live worker legitimately claimed a
  moment earlier.
- **The sweep decides whether to `task.delay()` from a fresh read made after winning its UPDATE**,
  never from the candidate-selection snapshot.

The last rule closes the one interleaving that would otherwise lose a task forever: the sweep
SELECTs a candidate whose re-queued row still exists (snapshot says "row exists → don't
re-queue"); while the sweep is still iterating its candidate list, that row fires, fails its
claim, and is consumed and deleted; the sweep's UPDATE then lands and leaves the task `PENDING`
with no queue row — a state no future sweep selects (candidates are `IN_PROGRESS` only). The
window is the sweep's whole SELECT→UPDATE gap, and it is widest exactly when recovery matters: a
crashed worker leaves many candidates (long iteration) and a backlogged queue leaves re-queued
rows sitting `todo` for minutes.

## Changes

### 1. Sweep logic — new `radis/core/utils/recovery.py`

`sweep_stale_analysis_state() -> None`.

- Iterate concrete `AnalysisTask` subclasses via `django.apps.apps.get_models()` filtered by
  `issubclass(m, AnalysisTask)` (currently `LabelingTask`, `ExtractionTask`, `SubscriptionTask`).
- Select candidates — task is `IN_PROGRESS` **and** the worker that was running it is gone.
  `ProcrastinateJob.status` is a plain `CharField` over `procrastinate.jobs.Status` values, and
  `ProcrastinateJob.worker` is a nullable FK to `ProcrastinateWorker.last_heartbeat`:

  ```python
  from datetime import timedelta

  from django.conf import settings
  from django.db.models import Q
  from django.utils import timezone
  from procrastinate.jobs import Status

  cutoff = timezone.now() - timedelta(seconds=settings.ANALYSIS_STALLED_WORKER_GRACE_SECONDS)

  owner_gone = (
      # queue row deleted (Procrastinate runs with --delete-jobs=always)
      Q(queued_job__isnull=True)
      # queue row is not being run by anyone: already re-queued, or finished
      | Q(
          queued_job__status__in=[
              Status.TODO.value,
              Status.SUCCEEDED.value,
              Status.FAILED.value,
              Status.CANCELLED.value,
              Status.ABORTED.value,
          ]
      )
      # queue row says doing, but its worker row was pruned
      | Q(queued_job__status=Status.DOING.value, queued_job__worker__isnull=True)
      # queue row says doing, but its worker stopped sending heartbeats
      | Q(queued_job__status=Status.DOING.value, queued_job__worker__last_heartbeat__lt=cutoff)
  )

  candidates = (
      Model.objects.filter(status=Model.Status.IN_PROGRESS)
      .filter(owner_gone)
      .select_related("job", "queued_job", "queued_job__worker")
  )
  ```

  Write this as positive `Q` disjunctions, **not** as `.exclude(<worker is alive>)`. With a nullable
  join, `NOT (queued_job.status = 'doing' AND …)` evaluates to NULL for a task whose queue row is
  gone, so those rows would be silently dropped — and orphans are the single most important case.
  `Status.ABORTING` is documented as legacy and unused, so it is deliberately absent; a row in any
  status other than `doing` is not being run by anyone.

- Resolve each with a **single conditional update that re-checks both the status and the
  owner-gone conditions at execution time** (Django compiles the joined conditions into the one
  `UPDATE` statement), because both worker containers sweep concurrently at deploy time, the
  periodic sweep ticks every minute, and a live worker may claim the task at any moment:

  ```python
  old_row_id = task.queued_job_id
  # Keep the link when the old row will fire again (todo/doing): the re-run task must still
  # point at its row, or the next sweep sees a NULL link, treats the running task as ownerless
  # and enqueues a second row for it. Drop the link only when a fresh row is enqueued below.
  keep_link = new_status == PENDING and row_will_refire(old_row_id)

  changes = {"status": new_status, "message": "The worker processing this task was terminated.",
             "ended_at": ended_at}
  if not keep_link:
      changes["queued_job_id"] = None
  updated = (
      Model.objects.filter(pk=task.pk, status=Model.Status.IN_PROGRESS)
      .filter(owner_gone)
      .update(**changes)
  )
  if not updated:
      continue  # another sweep won the race, or a live worker claimed the task meanwhile
  ```

  Only the sweep whose update changed a row proceeds. `new_status` is `CANCELED` when
  `task.job.status` is `CANCELING`/`CANCELED`, else `PENDING`. For `PENDING`, clear `ended_at`
  instead of setting it — the task has not ended.

- **Re-queue from a fresh read, only when there is no live row.** After a winning update whose
  outcome is `PENDING`, query the captured `stale_job_id` again: if that row no longer exists or
  is terminal, call `task.delay()`; if a live row (`todo` or `doing`) remains, do **not** — that
  row will fire and the processor's claim will find the task `PENDING`. Never decide from the
  candidate-selection snapshot: the row can fire, fail its claim, and be deleted while the sweep
  is still iterating, and a `PENDING` task with no row is invisible to every future sweep (see
  "Why every ordering converges").
- Collect affected jobs (deduped) and call `job.update_job_state()` on each non-terminal one.
- Never write to Procrastinate's own rows. They are exposed to Django as read-only models
  (`ProcrastinateReadOnlyModelMixin`) and are driven by Procrastinate's SQL state machine, so editing
  them directly risks breaking its invariants. Creating a _new_ row through the public API —
  `task.delay()`, i.e. `app.configure_task(...).defer(...)` — is fine and is what the re-queue rule
  above requires; the prohibition is on hand-editing rows that already exist.
- Log a single summary line, not one line per task — the sweep runs at container boot and a large
  recovery would otherwise bury the worker's startup output. Include per-model counts split by
  outcome, e.g.
  `Swept stale analysis state: LabelingTask 3 (2 pending, 1 canceled), ExtractionTask 0, SubscriptionTask 1 (1 pending)`.

Do **not** call `app.job_manager.retry_job_by_id()`: `procrastinate_retry_job_v2` raises unless the
row is `doing`/`failed`, so it races the shared periodic retry.

### 2. Command — new `radis/core/management/commands/sweep_stale_tasks.py`

Calls `sweep_stale_analysis_state()`. **Must catch all exceptions, log them, and exit 0.** It runs in
front of `bg_worker` joined by `&&`; a non-zero exit would stop the worker from starting at all, and
Swarm would exhaust `max_attempts` failing identically, leaving no workers. A repair that did not
happen is recoverable; a worker that will not boot is not.

Mirror the terse style of `adit_radis_shared/common/management/commands/retry_stalled_jobs.py`.

### 3. Settings — `radis/settings/base.py`

Next to `STALLED_JOBS_RETRY_PRIORITY` (~line 568):

```python
ANALYSIS_STALLED_WORKER_GRACE_SECONDS = env.int("ANALYSIS_STALLED_WORKER_GRACE_SECONDS", default=30)
ANALYSIS_SWEEP_CRON = env.str("ANALYSIS_SWEEP_CRON", default="* * * * *")
```

Document both in `example.env` and the env-var section of `AGENTS.md` (`CLAUDE.md` is a symlink to
it), including the constraint that the grace must not be set below 30.

### 4. Compose — worker start commands

`docker-compose.prod.yml` (`default_worker` lines 57-66, `llm_worker` lines 67-76):

```bash
wait-for-it -s postgres.local:5432 -t ${WAIT_POSTGRES_TIMEOUT:-180} &&
wait-for-it -s init.local:8000 -t 300 &&
./manage.py sweep_stale_tasks &&
./manage.py bg_worker -q <queue>
```

`docker-compose.dev.yml` (workers at lines 63-79): same, but wait on `web.local:8000` — dev disables
`init` (`profiles: never`) and runs `migrate` inside `web`.

The `init`/`web` wait closes a pre-existing gap: workers currently wait only for postgres and can
start mid-migration. It also makes ordering deterministic — `retry_stalled_jobs` runs inside
`init`/`web` before the port opens, so by sweep time stalled rows are already `todo`.

### 5. Processor — `radis/core/processors.py`

Replace `assert task.status == task.Status.PENDING` (line 37) **and** the unconditional
`IN_PROGRESS` save (lines 49-51) with a single atomic claim. The existing cancel branch at lines
25-35 already handles `CANCELING`/`CANCELED` jobs and stays first:

```python
now = timezone.now()
claimed = type(task).objects.filter(pk=task.pk, status=AnalysisTask.Status.PENDING).update(
    status=AnalysisTask.Status.IN_PROGRESS, started_at=now
)
if not claimed:
    # Stale IN_PROGRESS left by a killed worker (the periodic sweep will repair and
    # re-queue it), or already resolved by a sweep before this row fired. Not ours to run.
    logger.warning("Task %s was not PENDING, skipping.", task)
    return
task.status = AnalysisTask.Status.IN_PROGRESS
task.started_at = now
```

- The claim is one `UPDATE … WHERE id = %s AND status = 'PE'` — check and set are indivisible, so
  the processor can never execute a task it did not just flip from `PENDING`, and the sweep
  (which only ever writes onto `IN_PROGRESS` rows) can never yank back a task a worker holds.
  Read-then-branch here (an assert, or a status `if`) would race the sweep: it decides on an
  in-memory copy that the sweep may have already overwritten.
- The processor repairs nothing. When the claim fails on a stale `IN_PROGRESS` task, the queue
  row is consumed as a no-op and deleted; the next sweep tick sees the orphan and re-queues it.
  Repair authority lives in exactly one place — the sweep.
- The in-memory fields are set after the claim so the `finally` block's full `save()` writes the
  same values the claim wrote.
- The job-level transition (lines 41-46) is unchanged and runs after the claim.

Leave `assert job.status == job.Status.IN_PROGRESS` (line 46) untouched.

### 6. `update_job_state()` — `radis/core/models.py:128-130`

The final-evaluation branch raises `AssertionError` when every task is `CANCELED` but the job is not
`CANCELING` (reachable when a job is already `CANCELED` and a task re-fires). Replace the raise with
`status = CANCELED`, `message = "All tasks canceled."`, save, `return False` so the finished-mail
block is skipped.

### 7. Resume safety

- `radis/extractions/processors.py:29` — iterate `task.instances.filter(is_processed=False)` instead
  of `task.instances.all()`, so the `assert not instance.is_processed` at line 51 cannot fire on a
  resumed task.
- `radis/subscriptions/` — `SubscribedItem.objects.create()`
  (`radis/subscriptions/processors.py:66`) has no uniqueness, so a resumed task shows the user the
  same report twice. Add a `UniqueConstraint` on `(subscription, report)` plus a migration, and
  switch the call to `get_or_create`.
- `radis.labels` needs nothing.

### 8. Prep re-entry guards

A prep task re-fired after a crash mid-preparation must not crash or duplicate work.

- `radis/extractions/tasks.py:35` — `assert job.status == ExtractionJob.Status.PENDING` raises when
  the job is `PREPARING`. Accept `PENDING` and `PREPARING` (the existing `job.tasks.exists()` branch
  at line 44 is already the resume path); for any other status log a warning and return.
- `radis/subscriptions/tasks.py:38` — `assert job.status == SubscriptionJob.Status.PREPARING` passes
  on re-fire, but re-running duplicates tasks. Convert to a log-and-return guard for non-`PREPARING`,
  and add `job.tasks.all().delete()` before task creation, mirroring `radis/labels/tasks.py:60`.
  Safe because tasks are only enqueued after the switch to `PENDING`.

### 9. DB-level `ON DELETE SET NULL` migrations

Procrastinate deletes rows via raw SQL, bypassing Django's `SET_NULL`, so `queued_job_id` can point
at a row that no longer exists. Only `extractions` has the fix. Mirror
`radis/extractions/migrations/0002_procrastinate_on_delete.py` (uses
`adit_radis_shared.common.utils.migration_utils.procrastinate_on_delete_sql`, depends on
`("procrastinate", "0028_add_cancel_states")`):

- `radis/labels/migrations/0002_procrastinate_on_delete.py` — `labelingjob`, `labelingtask`;
  depends on `("labels", "0001_initial")`.
- `radis/subscriptions/migrations/0012_procrastinate_on_delete.py` — `subscriptionjob`,
  `subscriptiontask`; depends on the unique-constraint migration from change 7.

This makes the sweep's `queued_job_id IS NULL` branch reliable.

### 10. Docs

Rewrite `KNOWLEDGE.md:55-72` ("Recovering a job stuck in CANCELING"): recovery is now automatic at
worker startup and within about a minute in steady state (periodic sweep); demote the shell snippet
to immediate manual recovery. Add `ANALYSIS_STALLED_WORKER_GRACE_SECONDS` and `ANALYSIS_SWEEP_CRON`
to the env-var list in `AGENTS.md` and `example.env`.

## Out of scope

- **`restart_policy.window` in `docker-compose.prod.yml:11-15`.** Unrelated crashes months apart may
  accumulate toward `max_attempts`. Behaves identically before and after this change.
- **A lock around `update_job_state()`.** Two tasks of one job finishing simultaneously already race
  on that read-modify-write today, and can double-send the completion mail. ADIT guards the
  equivalent with `pglock.advisory` (`adit/core/tasks.py:167`); RADIS has no equivalent. Pre-existing,
  distinct fix, raise separately.
- **A shared implementation for ADIT.** ADIT has the same three tables, the same `queued_job`
  OneToOne (`adit/core/models.py:397`), the same shared `retry_stalled_jobs`, and the same
  workers-don't-wait-for-`init` gap (`docker-compose.prod.yml:64, 74, 85`), so the design transfers —
  but the model layer differs (`DicomJob`/`DicomTask`, `post_process()`, `start`/`end`), so the code
  does not. Factoring this into `adit-radis-shared` should not gate this fix.
- **A per-task attempt cap.** See below.

## Accepted risk

**A stale `IN_PROGRESS` is visible in the UI for up to ~90 s.** Death is only detectable after
30 s of heartbeat silence, so a window where the UI shows `IN_PROGRESS` with no worker on the task
is inherent to any design; with the 1-minute sweep it lasts roughly 30–90 s, after which the task
honestly shows `PENDING` until re-run. It is unbounded only if `default_worker` itself stays dead
(no periodic ticks), where repair waits for the next container boot — a state that needs a human
anyway. Today the same stale status lasts forever.

A task whose own content kills the worker (a batch large enough to exhaust container memory) is
resumed, crashes again, and repeats about every 10 minutes. Swarm's `max_attempts` bounds this
badly: what stops is the whole worker service, not the one task, and a redeploy resets the counter
while the task is still in the database.

We are not guarding against this. It requires a task that reliably kills a worker, which is
speculative for text batches; the failure is loud and corrupts nothing. If it occurs, the fix is a
per-task attempt counter — `AnalysisTask.attempts` already exists unused
(`radis/core/models.py:241`) — incremented at task **start** (a killed worker never reaches the end,
as ADIT does at `adit/core/tasks.py:83-86`) with a cap strictly below Swarm's `max_attempts`, so the
task gives up before the service does.

## Tests

Create `ProcrastinateJob`/`ProcrastinateWorker` rows directly
(`procrastinate.contrib.django.models`); use `time_machine` for heartbeat ages; use the existing
factory pattern from `radis/core/tests/test_processors.py`.

**New `radis/core/tests/test_recovery.py`:**

1. `IN_PROGRESS` task, `queued_job` NULL, job `CANCELING` → task `CANCELED`, job `CANCELED`
   (the reported bug).
2. `IN_PROGRESS` task, `queued_job` NULL, job `IN_PROGRESS` → task `PENDING`, `task.delay()` called
   (a new queue row exists), job not terminal.
3. Queue row `todo`, worker heartbeat stale → task `PENDING`, **no** new queue row created.
4. Queue row `doing`, worker heartbeat stale → task `PENDING`, no new row, Procrastinate row
   untouched.
5. Queue row `doing`, worker heartbeat fresh → task untouched (the long-LLM-batch guarantee).
6. Queue row `doing`, `worker_id` NULL → treated as dead.
7. Queue row terminal (`succeeded`/`failed`) → resolved like an orphan, new row created.
8. `PENDING` and terminal tasks are never touched.
9. Concurrency: run the resolve step twice against the same task; the second changes nothing and
   does not create a second queue row.
10. The command wrapper returns exit code 0 when `sweep_stale_analysis_state` raises.
11. Race — fresh-read re-queue: candidate selected while its `todo` row exists; delete the row
    before the resolve step runs → resolve sets `PENDING` **and** calls `task.delay()` (snapshot
    would have said "row exists, don't re-queue").
12. Race — owner-gone re-check: task `IN_PROGRESS` whose row is `doing` under a fresh heartbeat by
    the time the resolve step executes → the conditional update matches nothing, task untouched.
13. Periodic wiring: `sweep_stale_tasks_periodic` calls `sweep_stale_analysis_state` and is
    registered with `settings.ANALYSIS_SWEEP_CRON`.

**`radis/core/tests/test_processors.py`:**

14. `PENDING` task → claim succeeds, `process_task` is called, the task ends `SUCCESS`, and
    `started_at` is stamped.
15. Re-fired task with `status=IN_PROGRESS` under a live job → claim fails, `process_task` is
    **not** called, the task row is untouched (repair belongs to the sweep), no exception.
16. Re-fired task with `status=IN_PROGRESS` under a `CANCELING` job → `CANCELED` via the existing
    cancel branch, `process_task` never called.
17. Re-fired task already `SUCCESS` → claim fails, no-op, no exception. Update
    `test_start_assertion_error_on_invalid_task_status` (lines 253-262), which currently expects
    `AssertionError`. `test_start_assertion_error_on_invalid_job_status` still passes unchanged.

**`radis/core/tests/test_models.py`:**

18. `update_job_state()` with all tasks `CANCELED` and job not `CANCELING` → job `CANCELED`, no
    exception, no mail sent.

**Per-app:**

19. Extractions: a task with some instances already `is_processed` re-runs without raising and does
    not reprocess them.
20. Subscriptions: running the same task twice creates one `SubscribedItem`, not two.
21. Subscriptions: re-fired prep job does not duplicate tasks.
22. Extractions: re-fired prep job in `PREPARING` does not raise.

**Labels end-to-end** (extend `radis/labels/tests/test_jobs.py`):

23. `LabelingJob` in `CANCELING` with a stale `IN_PROGRESS` task → run the sweep → job `CANCELED`
    and a new `LabelingJob` can be created (singleton index unblocked).

## Verification

1. `uv run cli test -- -k "recovery or processor or update_job_state"`, then full `uv run cli test`.
2. `uv run ./manage.py makemigrations --check` — no auto-generated migrations beyond the
   hand-written `RunSQL` ones and the subscriptions unique constraint.
3. `uv run cli lint`.
4. Manual, dev compose — the reported bug:
   start a labeling backfill, `docker kill -s KILL` the `llm_worker` mid-task, cancel the job (it
   parks at `CANCELING`), restart the worker. The job must reach `CANCELED` unaided and a new
   labeling job must be startable, with no shell recovery.
5. Manual — resume: repeat without cancelling. Once with a restart delayed past 30 s (exercises the
   startup sweep), once with an immediate restart (exercises the periodic sweep: either it repairs
   the task before the re-queued row fires, or the row no-ops on a failed claim and the next tick
   repairs the orphan). The task must run again and the job complete in both.

## Key files

- `radis/core/utils/recovery.py` (new) — sweep logic
- `radis/core/management/commands/sweep_stale_tasks.py` (new) — command, never exits non-zero
- `radis/core/tasks.py` (new) — periodic sweep task (`ANALYSIS_SWEEP_CRON`)
- `radis/core/processors.py:25-51` — cancel branch, then the atomic claim replacing the assert
- `radis/core/models.py:92-130` — `update_job_state()`, incl. the all-canceled branch
- `radis/extractions/processors.py:29,51`, `radis/subscriptions/processors.py:66` — resume safety
- `radis/extractions/tasks.py:35`, `radis/subscriptions/tasks.py:38` — prep guards
- `docker-compose.dev.yml:63-79`, `docker-compose.prod.yml:57-76` — worker start commands
- `radis/labels/migrations/0002_procrastinate_on_delete.py`,
  `radis/subscriptions/migrations/0012_procrastinate_on_delete.py` (new)
- `radis/settings/base.py`, `example.env`, `AGENTS.md`, `KNOWLEDGE.md`
