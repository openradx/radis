# Labeling admin: error visibility, job cancel, deletion fix — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the `radis.labels` Django admin show why a labeling task warned (per-report failures), let operators cancel a running labeling job, and fix job deletion (currently blocked by the read-only task admin) while guarding active jobs.

**Architecture:** Three independent changes against the existing labels admin and the shared analysis-job framework. (A) The task processor persists per-report failures to the existing `AnalysisTask.log`/`message` fields and the admin surfaces them. (B) The canonical cancel logic is extracted from `AnalysisJobCancelView` into a reusable `cancel_job()` helper and invoked from a new admin cancel view + detail-page button. (C) The task admin permits cascade deletion and the job admin blocks deleting active-status jobs.

**Tech Stack:** Python 3.12, Django 6, Django admin, Procrastinate (task queue), pytest + pytest-django, factory-boy.

## Global Constraints

- Line length: 100 chars (Ruff). Style: Google Python Style Guide.
- Type checking: pyright basic mode.
- Run tests with `uv run cli test -- <args>` (pytest under the hood).
- Lint/format before committing: `uv run cli lint` and `uv run cli format-code`.
- No new model fields or migrations — only the existing `AnalysisTask.message` and `AnalysisTask.log` text fields are used.
- Cancel semantics are standard: pending tasks are canceled + their queued jobs revoked; an already-running task finishes its batch, then the job settles to `CANCELED`. No mid-batch abort.
- `LabelingJob.ACTIVE_STATUSES = (UNVERIFIED, PREPARING, PENDING, IN_PROGRESS, CANCELING)`.
- `AnalysisJob.is_cancelable` is `True` for `PENDING`, `PREPARING`, `IN_PROGRESS`.

---

## File Structure

- `radis/labels/processors.py` — Task A: collect + persist per-report failures.
- `radis/labels/admin.py` — Task A (task list/detail), Task B (cancel view/URL/template hook), Task C (delete permissions, bulk-action removal).
- `radis/core/utils/model_utils.py` — Task B: new shared `cancel_job()` helper.
- `radis/core/views.py` — Task B: `AnalysisJobCancelView.post` delegates to `cancel_job()`; drop now-unused `app` import.
- `radis/labels/templates/admin/labels/labelingjob/change_form.html` — Task B: new, "Cancel job" button.
- Tests: `radis/labels/tests/test_processor.py`, `radis/labels/tests/test_admin.py`, `radis/core/tests/test_models.py`.

---

## Task 1: Persist per-report failures in the labeling processor (Part A)

**Files:**
- Modify: `radis/labels/processors.py`
- Test: `radis/labels/tests/test_processor.py`

**Interfaces:**
- Consumes: `radis.labels.labeling.label_report(report_id)` (raises on failure), `LabelingTask.reports` M2M, `AnalysisTask.Status`, `AnalysisTask.message`, `AnalysisTask.log`.
- Produces: `LabelingTaskProcessor.process_task` now writes a structured failure block to `task.log` and sets `task.message = "<N> of <M> reports failed to label."` when any report fails. `_safe_label(report_id) -> tuple[int, str] | None` (returns `None` on success, `(report_id, "<ErrorClass>: <msg>")` on failure). Module constant `_MAX_LOGGED_FAILURES = 200`.

- [ ] **Step 1: Write the failing tests**

Add to `radis/labels/tests/test_processor.py`:

```python
@pytest.mark.django_db(transaction=True)
def test_processor_persists_per_report_failures_to_log(monkeypatch):
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
    assert task.status == AnalysisTask.Status.WARNING
    assert task.message == "1 of 2 reports failed to label."
    assert f"Report {r_bad.pk}: RuntimeError: LLM exploded" in task.log
    assert f"Report {r_ok.pk}:" not in task.log


@pytest.mark.django_db(transaction=True)
def test_processor_truncates_large_failure_log(monkeypatch):
    from radis.labels import processors

    monkeypatch.setattr(processors, "_MAX_LOGGED_FAILURES", 2)

    def fake_label_report(rid):
        raise RuntimeError("boom")

    monkeypatch.setattr(processors, "label_report", fake_label_report)

    reports = [ReportFactory.create() for _ in range(4)]
    job = LabelingJobFactory.create(status=LabelingJob.Status.PENDING)
    task = LabelingTaskFactory.create(job=job, status=AnalysisTask.Status.PENDING)
    task.reports.add(*reports)

    processors.LabelingTaskProcessor(task).start()

    task.refresh_from_db()
    assert task.status == AnalysisTask.Status.WARNING
    assert task.message == "4 of 4 reports failed to label."
    assert task.log.count("Report ") == 2
    assert "… and 2 more" in task.log
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run cli test -- radis/labels/tests/test_processor.py -k "per_report_failures or truncates" -v`
Expected: FAIL — current code sets `task.message = "Some reports failed to label; see logs."` and leaves `task.log` empty (assertions on message/log fail); `_MAX_LOGGED_FAILURES` does not exist (AttributeError on the monkeypatch in the truncation test).

- [ ] **Step 3: Rewrite `processors.py` to collect and persist failures**

Replace the body of `radis/labels/processors.py` (keep the existing imports) so it reads:

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

# Cap how many per-report failure lines are written to task.log so a fully-failing
# batch (up to LABELING_TASK_BATCH_SIZE reports) cannot bloat the row.
_MAX_LOGGED_FAILURES = 200


class LabelingTaskProcessor(AnalysisTaskProcessor):
    def process_task(self, task: LabelingTask) -> None:
        total = 0
        failures: list[tuple[int, str]] = []
        with ThreadPoolExecutor(max_workers=settings.LABELING_LLM_CONCURRENCY_LIMIT) as executor:
            try:
                futures: list[Future] = []
                for report_id in task.reports.values_list("pk", flat=True):
                    total += 1
                    futures.append(executor.submit(self._safe_label, report_id))
                for future in futures:
                    failure = future.result()
                    if failure is not None:
                        failures.append(failure)
            finally:
                db.close_old_connections()

        if failures:
            task.status = AnalysisTask.Status.WARNING
            task.message = f"{len(failures)} of {total} reports failed to label."
            task.log = self._format_failure_log(failures)

    @staticmethod
    def _format_failure_log(failures: list[tuple[int, str]]) -> str:
        lines = [
            f"Report {report_id}: {error}"
            for report_id, error in failures[:_MAX_LOGGED_FAILURES]
        ]
        remaining = len(failures) - _MAX_LOGGED_FAILURES
        if remaining > 0:
            lines.append(f"… and {remaining} more")
        return "\n".join(lines)

    def _safe_label(self, report_id: int) -> tuple[int, str] | None:
        try:
            label_report(report_id)
            return None
        except Exception as err:
            logger.exception("Labeling failed for report %s", report_id)
            return report_id, f"{type(err).__name__}: {err}"
        finally:
            db.close_old_connections()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run cli test -- radis/labels/tests/test_processor.py -v`
Expected: PASS — all processor tests, including the pre-existing `test_processor_one_report_failure_yields_warning` (it only asserts `task.message` is truthy, still satisfied).

- [ ] **Step 5: Lint and commit**

```bash
uv run cli format-code
uv run cli lint
git add radis/labels/processors.py radis/labels/tests/test_processor.py
git commit -m "feat(labels): persist per-report labeling failures to task log

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 2: Surface task failures in the LabelingTask admin (Part A)

**Files:**
- Modify: `radis/labels/admin.py` (`LabelingTaskAdmin`)
- Test: `radis/labels/tests/test_admin.py`

**Interfaces:**
- Consumes: `LabelingTask.message`, `LabelingTask.log` (populated by Task 1), `LabelingTask.reports`.
- Produces: `LabelingTaskAdmin.list_display` includes `message`. The read-only task detail page (reachable via Django view permission) renders `message`, `log`, and `reports`.

- [ ] **Step 1: Write the failing test**

Add to `radis/labels/tests/test_admin.py`:

```python
@_no_toolbar
@pytest.mark.django_db
def test_labeling_task_detail_shows_message_and_log(client):
    from radis.core.models import AnalysisTask
    from radis.labels.factories import LabelingJobFactory, LabelingTaskFactory

    job = LabelingJobFactory.create(status=LabelingJob.Status.WARNING)
    task = LabelingTaskFactory.create(
        job=job,
        status=AnalysisTask.Status.WARNING,
        message="1 of 2 reports failed to label.",
        log="Report 42: RuntimeError: LLM exploded",
    )
    admin_user = UserFactory.create(is_staff=True, is_superuser=True, is_active=True)
    client.force_login(admin_user)

    url = reverse("admin:labels_labelingtask_change", args=[task.pk])
    response = client.get(url)

    assert response.status_code == 200
    assert b"1 of 2 reports failed to label." in response.content
    assert b"Report 42: RuntimeError: LLM exploded" in response.content
```

- [ ] **Step 2: Run the test to verify it fails (or confirm the gap)**

Run: `uv run cli test -- radis/labels/tests/test_admin.py -k task_detail_shows_message -v`
Expected: PASS for status 200 is possible, but the assertion intent is to lock in that the detail page is reachable and shows the fields. If it fails (e.g. non-200), that confirms the page is unreachable. Either way proceed to Step 3 to add `message` to the changelist (the user-visible part) and keep this test as a regression guard.

- [ ] **Step 3: Add `message` to the task changelist**

In `radis/labels/admin.py`, change `LabelingTaskAdmin.list_display`:

```python
@admin.register(LabelingTask)
class LabelingTaskAdmin(_ReadOnlyAdmin):
    list_display = ("id", "job", "status", "message", "started_at", "ended_at")
    list_filter = ("status",)
    # Inert under read-only; kept for consistency with the other label admins so a future
    # editable admin degrades to an ID input rather than a full LabelingJob dropdown.
    raw_id_fields = ("job",)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run cli test -- radis/labels/tests/test_admin.py -v`
Expected: PASS.

- [ ] **Step 5: Lint and commit**

```bash
uv run cli format-code
uv run cli lint
git add radis/labels/admin.py radis/labels/tests/test_admin.py
git commit -m "feat(labels): surface task message and log in the task admin

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 3: Extract shared `cancel_job()` helper (Part B prerequisite)

**Files:**
- Modify: `radis/core/utils/model_utils.py`
- Modify: `radis/core/views.py` (`AnalysisJobCancelView.post`; remove unused `app` import)
- Test: `radis/core/tests/test_models.py`

**Interfaces:**
- Consumes: `AnalysisJob.tasks` (related manager), `job.tasks.model.Status`, `AnalysisJob.Status`, `procrastinate.contrib.django.app.job_manager.cancel_job_by_id`.
- Produces: `radis.core.utils.model_utils.cancel_job(job: AnalysisJob) -> None` — revokes pending tasks' queued jobs, marks pending tasks `CANCELED`, sets the job to `CANCELING` (if any task is `IN_PROGRESS`) else `CANCELED`, and saves. Used by Task 4.

- [ ] **Step 1: Write the failing test**

Add to `radis/core/tests/test_models.py` (use an existing concrete subclass for testing — `LabelingJob`/`LabelingTask` via their factories):

```python
@pytest.mark.django_db
def test_cancel_job_cancels_pending_and_sets_canceled():
    from radis.core.models import AnalysisTask
    from radis.core.utils.model_utils import cancel_job
    from radis.labels.factories import LabelingJobFactory, LabelingTaskFactory
    from radis.labels.models import LabelingJob

    job = LabelingJobFactory.create(status=LabelingJob.Status.PENDING)
    task = LabelingTaskFactory.create(job=job, status=AnalysisTask.Status.PENDING)

    cancel_job(job)

    job.refresh_from_db()
    task.refresh_from_db()
    assert job.status == LabelingJob.Status.CANCELED
    assert task.status == AnalysisTask.Status.CANCELED


@pytest.mark.django_db
def test_cancel_job_with_running_task_sets_canceling():
    from radis.core.models import AnalysisTask
    from radis.core.utils.model_utils import cancel_job
    from radis.labels.factories import LabelingJobFactory, LabelingTaskFactory
    from radis.labels.models import LabelingJob

    job = LabelingJobFactory.create(status=LabelingJob.Status.IN_PROGRESS)
    LabelingTaskFactory.create(job=job, status=AnalysisTask.Status.IN_PROGRESS)
    pending = LabelingTaskFactory.create(job=job, status=AnalysisTask.Status.PENDING)

    cancel_job(job)

    job.refresh_from_db()
    pending.refresh_from_db()
    assert job.status == LabelingJob.Status.CANCELING
    assert pending.status == AnalysisTask.Status.CANCELED
```

(If `test_models.py` lacks `import pytest`, add it.)

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run cli test -- radis/core/tests/test_models.py -k cancel_job -v`
Expected: FAIL — `ImportError: cannot import name 'cancel_job'`.

- [ ] **Step 3: Add `cancel_job()` to `model_utils.py`**

Edit `radis/core/utils/model_utils.py`:

```python
from typing import TYPE_CHECKING

from django.db import models
from procrastinate.contrib.django import app

if TYPE_CHECKING:
    from ..models import AnalysisJob, AnalysisTask


def reset_tasks(tasks: models.QuerySet["AnalysisTask"]) -> None:
    tasks.update(
        status=tasks.model.Status.PENDING,
        queued_job_id=None,
        attempts=0,
        message="",
        log="",
        started_at=None,
        ended_at=None,
    )


def cancel_job(job: "AnalysisJob") -> None:
    """Cancel a job: revoke pending tasks' queued jobs and mark them canceled.

    A task already in progress is left to finish its batch; the job then settles
    to CANCELED via update_job_state. The job moves to CANCELING while any task is
    still running, else straight to CANCELED.
    """
    task_status = job.tasks.model.Status
    pending = job.tasks.filter(status=task_status.PENDING)
    for task in pending.only("queued_job_id"):
        if task.queued_job_id is not None:
            app.job_manager.cancel_job_by_id(task.queued_job_id, delete_job=True)
    pending.update(status=task_status.CANCELED)

    if job.tasks.filter(status=task_status.IN_PROGRESS).exists():
        job.status = job.Status.CANCELING
    else:
        job.status = job.Status.CANCELED
    job.save()
```

- [ ] **Step 4: Refactor `AnalysisJobCancelView.post` to delegate**

In `radis/core/views.py`, update the import on line 30 and the `post` body (lines ~191-212):

Change:
```python
from radis.core.utils.model_utils import reset_tasks
```
to:
```python
from radis.core.utils.model_utils import cancel_job, reset_tasks
```

Replace the body of `AnalysisJobCancelView.post` with:
```python
    def post(self, request: AuthenticatedHttpRequest, *args, **kwargs) -> HttpResponse:
        job = cast(AnalysisJob, self.get_object())
        if not job.is_cancelable:
            raise SuspiciousOperation(
                f"Job with ID {job.pk} and status {job.get_status_display()} is not cancelable."
            )

        cancel_job(job)

        messages.success(request, self.success_message % job.__dict__)
        return redirect(job)
```

Then remove the now-unused import on line 28:
```python
from procrastinate.contrib.django import app
```
(Verify with `grep -n "app\." radis/core/views.py` returning nothing before deleting.)

- [ ] **Step 5: Run the focused + regression tests**

Run: `uv run cli test -- radis/core/tests/test_models.py -k cancel_job -v`
Expected: PASS.

Run: `uv run cli test -- radis/core radis/extractions -v`
Expected: PASS — `AnalysisJobCancelView` behaves identically (extractions' `ExtractionJobCancelView` subclasses it).

- [ ] **Step 6: Lint and commit**

```bash
uv run cli format-code
uv run cli lint
git add radis/core/utils/model_utils.py radis/core/views.py radis/core/tests/test_models.py
git commit -m "refactor(core): extract shared cancel_job helper from cancel view

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 4: Cancel button + view for labeling jobs (Part B)

**Files:**
- Modify: `radis/labels/admin.py` (`LabelingJobAdmin`)
- Create: `radis/labels/templates/admin/labels/labelingjob/change_form.html`
- Test: `radis/labels/tests/test_admin.py`

**Interfaces:**
- Consumes: `radis.core.utils.model_utils.cancel_job` (Task 3), `LabelingJob.is_cancelable`, `LabelingJob.get_status_display`.
- Produces: admin URL name `admin:labels_labelingjob_cancel` (kwarg `job_id`); a "Cancel job" button on the job change page rendered only when `original.is_cancelable`.

- [ ] **Step 1: Write the failing tests**

Add to `radis/labels/tests/test_admin.py`:

```python
@_no_toolbar
@pytest.mark.django_db
def test_cancel_job_view_cancels_cancelable_job(client):
    from radis.core.models import AnalysisTask
    from radis.labels.factories import LabelingJobFactory, LabelingTaskFactory

    job = LabelingJobFactory.create(status=LabelingJob.Status.PENDING)
    task = LabelingTaskFactory.create(job=job, status=AnalysisTask.Status.PENDING)
    admin_user = UserFactory.create(is_staff=True, is_superuser=True, is_active=True)
    client.force_login(admin_user)

    url = reverse("admin:labels_labelingjob_cancel", args=[job.pk])
    response = client.post(url)

    assert response.status_code == 302
    job.refresh_from_db()
    task.refresh_from_db()
    assert job.status == LabelingJob.Status.CANCELED
    assert task.status == AnalysisTask.Status.CANCELED


@_no_toolbar
@pytest.mark.django_db
def test_cancel_job_view_rejects_finished_job(client):
    from radis.labels.factories import LabelingJobFactory

    job = LabelingJobFactory.create(status=LabelingJob.Status.SUCCESS)
    admin_user = UserFactory.create(is_staff=True, is_superuser=True, is_active=True)
    client.force_login(admin_user)

    url = reverse("admin:labels_labelingjob_cancel", args=[job.pk])
    response = client.post(url)

    assert response.status_code == 302
    job.refresh_from_db()
    assert job.status == LabelingJob.Status.SUCCESS


@_no_toolbar
@pytest.mark.django_db
def test_cancel_button_shown_only_when_cancelable(client):
    from radis.labels.factories import LabelingJobFactory

    admin_user = UserFactory.create(is_staff=True, is_superuser=True, is_active=True)
    client.force_login(admin_user)

    active = LabelingJobFactory.create(status=LabelingJob.Status.IN_PROGRESS)
    done = LabelingJobFactory.create(status=LabelingJob.Status.SUCCESS)

    active_url = reverse("admin:labels_labelingjob_change", args=[active.pk])
    done_url = reverse("admin:labels_labelingjob_change", args=[done.pk])

    assert b"Cancel job" in client.get(active_url).content
    assert b"Cancel job" not in client.get(done_url).content
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run cli test -- radis/labels/tests/test_admin.py -k cancel -v`
Expected: FAIL — `NoReverseMatch: 'labels_labelingjob_cancel'` (URL not registered yet).

- [ ] **Step 3: Add the cancel URL + view and wire the template**

In `radis/labels/admin.py`, add the shared import near the top (with the other imports):

```python
from radis.core.utils.model_utils import cancel_job
```

In `LabelingJobAdmin`, set the change-form template and add the route + view. Update `change_form_template`, `get_urls`, and add `cancel_job_view`:

```python
@admin.register(LabelingJob)
class LabelingJobAdmin(admin.ModelAdmin):
    # Uses a custom changelist template that adds a "Run backfill now" button so that
    # the action does not require selecting a row (Django's built-in action mechanism
    # enforces at least one selected object).
    change_list_template = "admin/labels/labelingjob/change_list.html"
    # Adds a "Cancel job" button to the read-only detail page when the job is cancelable.
    change_form_template = "admin/labels/labelingjob/change_form.html"

    list_display = ("id", "trigger", "status", "owner", "created_at", "ended_at")
    list_filter = ("trigger", "status")
```

(Leave the existing `readonly_fields` and `has_add_permission` as they are.)

Extend `get_urls`:

```python
    def get_urls(self):
        urls = super().get_urls()
        custom = [
            path(
                "run-backfill/",
                self.admin_site.admin_view(self.run_backfill_view),
                name="labels_labelingjob_run_backfill",
            ),
            path(
                "<int:job_id>/cancel/",
                self.admin_site.admin_view(self.cancel_job_view),
                name="labels_labelingjob_cancel",
            ),
        ]
        return custom + urls
```

Add the view method (next to `run_backfill_view`):

```python
    def cancel_job_view(self, request: HttpRequest, job_id: int) -> HttpResponseRedirect:
        change_url = reverse("admin:labels_labelingjob_change", args=[job_id])
        if request.method != "POST":
            return HttpResponseRedirect(change_url)
        job = LabelingJob.objects.get(pk=job_id)
        if not job.is_cancelable:
            self.message_user(
                request,
                f"Job {job.pk} with status {job.get_status_display()} is not cancelable.",
                level=messages.ERROR,
            )
            return HttpResponseRedirect(change_url)
        cancel_job(job)
        self.message_user(request, f"Canceling job {job.pk}.", level=messages.SUCCESS)
        return HttpResponseRedirect(change_url)
```

- [ ] **Step 4: Create the change-form template**

Create `radis/labels/templates/admin/labels/labelingjob/change_form.html`:

```django
{% extends "admin/change_form.html" %}
{% load i18n %}
{% block submit_buttons_bottom %}
    {{ block.super }}
    {% if original.is_cancelable %}
        <form method="post"
              action="{% url 'admin:labels_labelingjob_cancel' original.pk %}"
              style="display:inline">
            {% csrf_token %}
            <button type="submit" class="button">{% trans "Cancel job" %}</button>
        </form>
    {% endif %}
{% endblock submit_buttons_bottom %}
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run cli test -- radis/labels/tests/test_admin.py -k cancel -v`
Expected: PASS.

- [ ] **Step 6: Lint and commit**

```bash
uv run cli format-code
uv run cli lint
git add radis/labels/admin.py radis/labels/templates/admin/labels/labelingjob/change_form.html radis/labels/tests/test_admin.py
git commit -m "feat(labels): add cancel button to the labeling job admin

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 5: Make job deletion work, but block active jobs (Part C)

**Files:**
- Modify: `radis/labels/admin.py` (`LabelingTaskAdmin`, `LabelingJobAdmin`)
- Test: `radis/labels/tests/test_admin.py`

**Interfaces:**
- Consumes: `LabelingJob.ACTIVE_STATUSES`, Django admin permission hooks.
- Produces: `LabelingTaskAdmin.has_delete_permission` → `True` (cascade allowed; add/change still `False`). `LabelingJobAdmin.has_delete_permission(request, obj)` → `False` for active-status jobs. `LabelingJobAdmin.get_actions` no longer offers `delete_selected`.

- [ ] **Step 1: Update the existing read-only test and add new deletion tests**

In `radis/labels/tests/test_admin.py`, the existing `test_read_only_admins_block_deletion` asserts `LabelingTask` blocks deletion — that is no longer true. Replace it and add new tests:

```python
@pytest.mark.django_db
def test_result_and_gate_admins_block_deletion():
    """LabelResult/GateAnswer admins stay fully read-only (no add/change/delete)."""
    from django.contrib.admin.sites import site
    from django.test import RequestFactory

    from radis.labels.models import GateAnswer, LabelResult

    request = RequestFactory().get("/")
    for model in (LabelResult, GateAnswer):
        model_admin = site._registry[model]
        assert model_admin.has_add_permission(request) is False
        assert model_admin.has_change_permission(request) is False
        assert model_admin.has_delete_permission(request) is False


@pytest.mark.django_db
def test_task_admin_allows_delete_but_not_add_or_change():
    """LabelingTask must allow deletion so a job delete can cascade to its tasks."""
    from django.contrib.admin.sites import site
    from django.test import RequestFactory

    from radis.labels.models import LabelingTask

    request = RequestFactory().get("/")
    model_admin = site._registry[LabelingTask]
    assert model_admin.has_add_permission(request) is False
    assert model_admin.has_change_permission(request) is False
    assert model_admin.has_delete_permission(request) is True


@pytest.mark.django_db
def test_job_admin_blocks_deleting_active_jobs():
    from django.contrib.admin.sites import site
    from django.test import RequestFactory

    from radis.labels.factories import LabelingJobFactory
    from radis.labels.models import LabelingJob

    request = RequestFactory().get("/")
    model_admin = site._registry[LabelingJob]

    active = LabelingJobFactory.create(status=LabelingJob.Status.IN_PROGRESS)
    done = LabelingJobFactory.create(status=LabelingJob.Status.SUCCESS)

    assert model_admin.has_delete_permission(request, active) is False
    assert model_admin.has_delete_permission(request, done) is True


@pytest.mark.django_db
def test_job_admin_has_no_bulk_delete_action():
    from django.contrib.admin.sites import site
    from django.test import RequestFactory

    from radis.labels.models import LabelingJob

    admin_user = UserFactory.create(is_staff=True, is_superuser=True, is_active=True)
    request = RequestFactory().get("/")
    request.user = admin_user

    model_admin = site._registry[LabelingJob]
    assert "delete_selected" not in model_admin.get_actions(request)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run cli test -- radis/labels/tests/test_admin.py -k "deletion or delete or read_only or task_admin or bulk" -v`
Expected: FAIL — `LabelingTask` delete is currently `False`; `LabelingJobAdmin.has_delete_permission` currently returns `True` for active jobs; `delete_selected` is still present.

- [ ] **Step 3: Allow task cascade deletion**

In `radis/labels/admin.py`, override `has_delete_permission` on `LabelingTaskAdmin`:

```python
@admin.register(LabelingTask)
class LabelingTaskAdmin(_ReadOnlyAdmin):
    list_display = ("id", "job", "status", "message", "started_at", "ended_at")
    list_filter = ("status",)
    # Inert under read-only; kept for consistency with the other label admins so a future
    # editable admin degrades to an ID input rather than a full LabelingJob dropdown.
    raw_id_fields = ("job",)

    def has_delete_permission(self, request: HttpRequest, obj: object = None) -> bool:
        # Allow deletion so deleting a LabelingJob can cascade to its tasks. Add/change
        # remain blocked via _ReadOnlyAdmin, so tasks are still effectively view-only.
        return True
```

- [ ] **Step 4: Guard active jobs and drop the bulk delete action**

In `radis/labels/admin.py`, add to `LabelingJobAdmin`:

```python
    def has_delete_permission(self, request: HttpRequest, obj: object = None) -> bool:
        # Active jobs must be canceled (which revokes their queued work) before deletion;
        # deleting a running job would orphan in-flight LLM calls. Finished jobs delete freely.
        if isinstance(obj, LabelingJob) and obj.status in LabelingJob.ACTIVE_STATUSES:
            return False
        return super().has_delete_permission(request, obj)

    def get_actions(self, request: HttpRequest):
        # Bulk "delete selected" checks delete permission once with no object, bypassing the
        # per-job active-status guard above. Remove it so deletion only happens per-object.
        actions = super().get_actions(request)
        actions.pop("delete_selected", None)
        return actions
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run cli test -- radis/labels/tests/test_admin.py -v`
Expected: PASS (all admin tests, including the rewritten read-only test).

- [ ] **Step 6: Run the full labels + core suite**

Run: `uv run cli test -- radis/labels radis/core -v`
Expected: PASS.

- [ ] **Step 7: Lint and commit**

```bash
uv run cli format-code
uv run cli lint
git add radis/labels/admin.py radis/labels/tests/test_admin.py
git commit -m "fix(labels): allow job deletion to cascade, guard active jobs

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Final verification

- [ ] Run the whole affected suite: `uv run cli test -- radis/labels radis/core radis/extractions -v` — all green.
- [ ] `uv run cli lint` — clean.
- [ ] Manual smoke (optional, dev containers): in the admin, run a backfill that hits a failing report → task shows `WARNING` with an `N of M` message and a per-report log; a running job shows a "Cancel job" button; canceling moves it to `CANCELING`/`CANCELED`; a finished job deletes with its tasks; an active job has no delete option and no bulk-delete action.
```
