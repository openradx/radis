# Labeling admin: error visibility, job cancel, and deletion fix

**Date:** 2026-06-26
**App:** `radis.labels`
**Status:** Approved design, ready for implementation plan

## Problem

The Django admin for auto-labeling gives operators no way to understand or
control jobs:

1. **No error visibility.** When a labeling task ends in `WARNING`, the admin
   shows no reason. Per-report failures are swallowed into the worker's stdout
   (`logger.exception`) and never persisted; the task's `message` is a generic
   "see logs" string that isn't even displayed, and the `log` field stays empty
   for warnings. Operators can't see *why* a task warned, *which* reports failed,
   or *what* the error was.
2. **No cancel.** A running backfill cannot be stopped cleanly. The framework
   already supports cancellation (`AnalysisJobCancelView`, `job.is_cancelable`,
   `CANCELING`/`CANCELED` statuses — already in `LabelingJob.ACTIVE_STATUSES`),
   but the labels admin exposes only a "Run backfill now" button.
3. **Deletion is blocked.** Attempting to delete a `LabelingJob` that has tasks
   fails — even for a superuser — with: *"Cannot delete labeling job …
   your account doesn't have permission to delete the following types of
   objects: labeling task."* Cause: `LabelingTaskAdmin` inherits `_ReadOnlyAdmin`,
   whose `has_delete_permission` returns `False` unconditionally, so the admin
   refuses the `CASCADE` from the job to its tasks.

## Goals

- Persist and surface per-report labeling failures in the admin.
- Add a cancel control for labeling jobs, reusing the framework cancel logic.
- Make job deletion work, while blocking deletion of active (still-running) jobs
  so Cancel is the path for those.

## Non-goals

- No live/real-time progress tracking (processed/total, currently-processing
  report). Out of scope by decision.
- No mid-batch cooperative abort of an already-running task. Cancel uses standard
  semantics: a running task finishes its batch.
- No changelist bulk "cancel selected" action. Cancel lives on the job detail
  page only.
- No new models, no schema migrations for labeling-results data. The only model
  fields used (`AnalysisTask.message`, `AnalysisTask.log`) already exist.
- No "revoke queued jobs on delete" behavior — active jobs simply can't be
  deleted; Cancel revokes and then the job becomes deletable.

## Current behavior (reference)

- `radis/labels/processors.py` — `LabelingTaskProcessor.process_task` runs
  `_safe_label(report_id)` per report in a `ThreadPoolExecutor`. `_safe_label`
  returns a bool, logging exceptions via `logger.exception` only. On any failure
  it sets `task.status = WARNING` and `task.message = "Some reports failed to
  label; see logs."` Nothing is written to `task.log`.
- `radis/core/processors.py` — the base `AnalysisTaskProcessor.start` only writes
  a traceback to `task.log` when `process_task` itself raises (a full task
  crash → `FAILURE`). Per-report warnings never reach `log`.
- `radis/labels/admin.py`:
  - `_ReadOnlyAdmin` overrides `has_add/change/delete_permission` → `False`.
  - `LabelResultAdmin`, `GateAnswerAdmin`, `LabelingTaskAdmin` extend it.
  - `LabelingTaskAdmin.list_display = ("id", "job", "status", "started_at",
    "ended_at")` — no `message`.
  - `LabelingJobAdmin` extends plain `ModelAdmin`, overrides only
    `has_add_permission` → `False`; all fields read-only; custom changelist
    template with "Run backfill now"; `get_urls()` adds `run-backfill/`.
- `radis/core/views.py` — `AnalysisJobCancelView.post` holds the canonical cancel
  logic: for pending tasks revoke `queued_job_id` via
  `app.job_manager.cancel_job_by_id(queued_job_id, delete_job=True)`, mark them
  `CANCELED`; then set the job to `CANCELING` if any task is `IN_PROGRESS`, else
  `CANCELED`.

## Design

### Part A — Persist per-report failures and surface them

**`radis/labels/processors.py`**

- Change `_safe_label` to report the failing report and its error instead of a
  bare bool. Return a value that lets `process_task` distinguish success from
  failure and carry `(report_id, error)` for failures (e.g. return `None` on
  success or a `(report_id, error)` tuple on failure; exact shape decided in the
  plan).
- In `process_task`, collect failures from the futures. When there are failures:
  - Build a structured block written to `task.log`, one line per failed report:
    `Report <id>: <ExceptionClassName>: <message>`.
  - Cap the block to a sane number of lines (e.g. first 200) and append a
    `… and N more` footer so a fully-failing batch can't bloat the row.
  - Set `task.message = "<N> of <M> reports failed to label."`
  - Keep `task.status = WARNING` (unchanged semantics).
- Keep the existing `logger.exception` per failure for live worker-log tailing.

**`radis/labels/admin.py` — `LabelingTaskAdmin`**

- Add `message` to `list_display`.
- Ensure the read-only detail page is reachable (renders via Django's view
  permission) and shows `message`, `log`, and the task's `reports` M2M (answers
  "which reports are in this task"). Tasks remain non-addable and
  non-changeable.

### Part B — Cancel button on the job detail page

Reuse the framework cancel; do not reimplement it.

**Shared cancel logic (refactor for one source of truth)**

- Extract the body of `AnalysisJobCancelView.post` into a reusable callable
  (e.g. a function/method that takes a job and performs: revoke pending tasks'
  queued jobs, mark them `CANCELED`, set job `CANCELING`/`CANCELED`, save).
- `AnalysisJobCancelView.post` calls it (behavior must stay identical).

**`radis/labels/admin.py` — `LabelingJobAdmin`**

- Add a `cancel/<job_id>/` route in `get_urls()` alongside `run-backfill/`,
  wired through `admin_site.admin_view`.
- `cancel_job_view`:
  - `POST` only; non-POST redirects to the job change page.
  - Load the job; if `not job.is_cancelable`, `message_user(..., ERROR)` and
    redirect back to the job change page.
  - Otherwise call the shared cancel logic, `message_user(..., SUCCESS)`, and
    redirect to the job change page.

**Template**

- Add `templates/admin/labels/labelingjob/change_form.html` extending the admin
  default, rendering a **"Cancel job"** button in the submit row, shown only when
  `original.is_cancelable`. The button POSTs to the cancel URL with CSRF.

### Part C — Make job deletion work, but block active jobs

**`radis/labels/admin.py` — `LabelingTaskAdmin`**

- Override `has_delete_permission` to return `True` (instead of inheriting
  `_ReadOnlyAdmin`'s `False`), so the job→task `CASCADE` is permitted. Tasks
  remain non-addable and non-changeable. Accepted side effect: standalone task
  deletion becomes possible (low risk; never the intended path).

**`radis/labels/admin.py` — `LabelingJobAdmin`**

- Override `has_delete_permission(request, obj=None)`: return `False` when `obj`
  is in `LabelingJob.ACTIVE_STATUSES`
  (`UNVERIFIED`/`PREPARING`/`PENDING`/`IN_PROGRESS`/`CANCELING`); otherwise
  default. Forces Cancel-first for running jobs; finished jobs
  (`SUCCESS`/`WARNING`/`FAILURE`/`CANCELED`) delete cleanly with their tasks.
- Disable the bulk "delete selected" action (remove `delete_selected` from
  actions). Django's bulk delete checks `has_delete_permission(request)` once
  with no object, bypassing the per-job active guard; removing it keeps deletion
  on the guarded per-object delete page only.

## Error handling

- `cancel_job_view`: non-cancelable job → user-facing error message, no state
  change; GET → redirect with no action.
- Deleting an active job → blocked by `has_delete_permission` (no delete button /
  blocked confirmation); the operator is steered to Cancel.
- Log truncation prevents oversized `task.log` rows on mass failure.

## Testing

**Part A — processor**
- A task where some reports fail persists one `log` line per failed report
  (`Report <id>: <Error>: <msg>`), sets `message = "N of M reports failed to
  label."`, and status `WARNING`.
- Truncation: more than the cap failing reports → first N lines plus
  `… and N more` footer.
- All-success task: no failure block, normal success status.

**Part B — cancel**
- Cancelable job → pending tasks `CANCELED`, their queued jobs revoked
  (`cancel_job_by_id(..., delete_job=True)`), job `CANCELING` when a task is
  `IN_PROGRESS` else `CANCELED`.
- Non-cancelable job → error message, no state change.
- GET to the cancel URL → no-op redirect.
- Shared-cancel refactor: `AnalysisJobCancelView` behaves identically to before.

**Part C — deletion**
- Finished job deletes and cascades to its tasks successfully.
- Active job: delete is blocked (no button / blocked confirmation page).
- Bulk "delete selected" action is absent from the job changelist.

## Files touched

- `radis/labels/processors.py` — persist per-report failures (A).
- `radis/labels/admin.py` — task list/detail (A); cancel URL + view (B); delete
  permissions and bulk-action removal (C).
- `radis/core/views.py` — extract shared cancel logic (B).
- `radis/labels/templates/admin/labels/labelingjob/change_form.html` — new,
  Cancel button (B).
- Tests under `radis/labels/tests/` and any affected `radis/core` cancel tests.
