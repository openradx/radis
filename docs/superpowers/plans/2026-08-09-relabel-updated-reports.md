# Re-label Reports When Their Content Changes — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reports whose content is edited after initial labeling get re-labeled — the scan window and every staleness check switch from label-side-only timestamps to also consider `Report.updated_at`.

**Architecture:** Two coordinated changes in the `radis.labels` app: (1) the incremental scan selects candidates by `updated_at` instead of `created_at`; (2) every freshness predicate (gate answers and label results, in the labeling engine, backfill scope, model property, admin, and status command) additionally requires `generated_at >= report.updated_at`. No schema changes, no migrations.

**Tech Stack:** Django 6 ORM (`OuterRef`/`Exists`/`F`/`Q` subqueries), Procrastinate tasks, pytest + pytest-django + factory-boy.

**Spec:** `docs/superpowers/specs/2026-08-09-relabel-updated-reports-design.md`

## Global Constraints

- Line length 100 (Ruff), Google Python Style, pyright basic mode.
- Keep docstrings/comments terse — match existing house style; comments only for constraints the code can't show.
- All work happens on the existing branch `relabel-updated-reports`.
- Test command: `uv run pytest <path> -v` from the project root (`/workspaces/adit-radis-workspace/projects/radis`). Tests marked `@pytest.mark.django_db` need the dev Postgres container running.
- Pre-commit hooks (ruff, pyright, djlint…) run automatically on `git commit`; a failed hook aborts the commit — fix and re-commit.
- Commit messages end with `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`.
- Timestamp mechanics the tests rely on: `Report.updated_at`, `GateAnswer.generated_at`, and `LabelResult.generated_at` are `auto_now=True`. A `report.save()` after creating a gate/result bumps `updated_at` past `generated_at` (microsecond precision, so consecutive saves are strictly ordered). `QuerySet.update(...)` bypasses `auto_now`, which is how tests backdate rows.

---

### Task 1: Scan window uses `updated_at`

The nightly scan and SCAN-job scope currently filter on `created_at`, so an edited report never enters a scan job.

**Files:**
- Modify: `radis/labels/tasks.py:21` (`_scope_queryset`) and `radis/labels/tasks.py:146` (`incremental_label_scan`)
- Test: `radis/labels/tests/test_jobs.py` (modify `test_scan_job_only_includes_reports_after_scan_from`, add one test)
- Test: `radis/labels/tests/test_scan.py` (add one test)

**Interfaces:**
- Consumes: `Report.updated_at` (exists on `radis.reports.models.Report`).
- Produces: SCAN jobs whose scope is `Report.objects.filter(updated_at__gte=job.scan_from)`. Later tasks don't depend on this task's code, only on the same convention.

- [ ] **Step 1: Fix the existing scope test so it stays valid under the new filter**

In `radis/labels/tests/test_jobs.py`, `test_scan_job_only_includes_reports_after_scan_from` backdates only `created_at` of the `old` report. Under the new `updated_at` filter, `old.updated_at` would still be "now" and the report would be included, failing the test for the wrong reason. Change the backdating line:

```python
    Report.objects.filter(pk=old.pk).update(
        created_at=timezone.now() - timedelta(days=10),
        updated_at=timezone.now() - timedelta(days=10),
    )
```

- [ ] **Step 2: Write the failing test for SCAN scope — updated old report is included**

Append to `radis/labels/tests/test_jobs.py` (same style as the neighboring test):

```python
@pytest.mark.django_db
def test_scan_job_includes_old_report_updated_after_scan_from(monkeypatch):
    from datetime import timedelta

    from django.utils import timezone

    from radis.labels import tasks
    from radis.labels.factories import LabelFactory, LabelGroupFactory
    from radis.labels.models import LabelingTask
    from radis.reports.factories import ReportFactory
    from radis.reports.models import Report

    LabelFactory.create(group=LabelGroupFactory.create())
    report = ReportFactory.create()
    # Created long before the cutoff, but updated after it (update() bypasses auto_now).
    Report.objects.filter(pk=report.pk).update(created_at=timezone.now() - timedelta(days=10))
    cutoff = timezone.now() - timedelta(days=1)

    job = LabelingJobFactory.create(
        trigger=LabelingJob.Trigger.SCAN, scan_from=cutoff, status=LabelingJob.Status.PENDING
    )
    monkeypatch.setattr(LabelingTask, "delay", lambda self: None)
    tasks.process_labeling_job(job.pk)

    included = set()
    for task in job.tasks.all():
        included.update(task.reports.values_list("pk", flat=True))
    assert report.pk in included
```

- [ ] **Step 3: Write the failing test for the scan tick — updated old report triggers a job**

Append to `radis/labels/tests/test_scan.py`:

```python
@pytest.mark.django_db
def test_updated_old_report_creates_scan_job(monkeypatch):
    from radis.labels import tasks
    from radis.reports.models import Report

    LabelFactory.create(group=LabelGroupFactory.create())
    report = ReportFactory.create()
    # Created before the checkpoint, updated after it.
    Report.objects.filter(pk=report.pk).update(created_at=timezone.now() - timedelta(days=10))
    LabelingScanCheckpoint.objects.create(last_scanned_at=timezone.now() - timedelta(hours=1))

    delayed = []
    monkeypatch.setattr(LabelingJob, "delay", lambda self: delayed.append(self.pk))
    tasks.incremental_label_scan(_now_ts())

    job = LabelingJob.objects.get(trigger=LabelingJob.Trigger.SCAN)
    assert delayed == [job.pk]
```

Note: `ReportFactory.create()` leaves `updated_at` = now, which is after the checkpoint — the backdated `created_at` is what proves the filter no longer keys on creation time.

- [ ] **Step 4: Run both new tests to verify they fail**

Run: `uv run pytest radis/labels/tests/test_jobs.py::test_scan_job_includes_old_report_updated_after_scan_from radis/labels/tests/test_scan.py::test_updated_old_report_creates_scan_job -v`
Expected: both FAIL (report not in scope / `LabelingJob.DoesNotExist`) because the code still filters on `created_at`.

- [ ] **Step 5: Implement — switch both filters to `updated_at`**

In `radis/labels/tasks.py`:

```python
def _scope_queryset(job: LabelingJob) -> QuerySet:
    if job.scan_from is not None:  # SCAN job: recent window
        return Report.objects.filter(updated_at__gte=job.scan_from).order_by("pk")
```

and in `incremental_label_scan`:

```python
    if Report.objects.filter(updated_at__gte=checkpoint.last_scanned_at).exists():
```

- [ ] **Step 6: Run the affected test files**

Run: `uv run pytest radis/labels/tests/test_jobs.py radis/labels/tests/test_scan.py -v`
Expected: all PASS.

- [ ] **Step 7: Commit**

```bash
git add radis/labels/tasks.py radis/labels/tests/test_jobs.py radis/labels/tests/test_scan.py
git commit -m "feat(labels): scan window keys on report updated_at, not created_at

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 2: `label_report` re-runs gate and labels when the report changed

Staleness inside the labeling engine only compares against label-side timestamps; an edited report's gate answers and results look fresh and everything is skipped.

**Files:**
- Modify: `radis/labels/labeling.py:47-51` (gate freshness) and `radis/labels/labeling.py:105-118` (`_get_stale_or_missing_labels`)
- Test: `radis/labels/tests/test_labeling.py` (add two tests)

**Interfaces:**
- Consumes: `report.updated_at` on the `Report` instance already loaded at the top of `label_report`.
- Produces: unchanged signatures — `label_report(report_id: int) -> None`, `_get_stale_or_missing_labels(report: Report, labels: list[Label]) -> list[Label]`. Only the freshness semantics change: fresh now means `generated_at >= label-side updated_at` AND `generated_at >= report.updated_at`.

- [ ] **Step 1: Write the failing tests**

Append to `radis/labels/tests/test_labeling.py` (uses the existing `FakeChatClient` / `_patch_client` helpers and `LabelResultFactory`; extend the imports at the top of the file to include `LabelResultFactory` from `radis.labels.factories`):

```python
@pytest.mark.django_db
def test_report_update_reruns_gate_and_labels():
    from radis.labels.labeling import label_report

    report = ReportFactory.create(body="original body")
    group = LabelGroupFactory.create()
    label = LabelFactory.create(group=group)
    GateAnswerFactory.create(report=report, label_group=group, value=GateAnswer.Value.YES)
    LabelResultFactory.create(report=report, label=label, value=LabelResult.Value.ABSENT)

    # Editing the report bumps report.updated_at past both generated_at values.
    report.body = "changed body"
    report.save()

    client = FakeChatClient(
        gate_values={group.name: "YES"}, label_values={label.name: "PRESENT"}
    )
    with _patch_client(client):
        label_report(report.pk)

    assert len(client.gate_calls) == 1
    assert len(client.label_calls) == 1
    assert LabelResult.objects.get(report=report, label=label).value == "PRESENT"


@pytest.mark.django_db
def test_report_update_reruns_gate_even_when_previous_answer_was_no():
    from radis.labels.labeling import label_report

    report = ReportFactory.create(body="original body")
    group = LabelGroupFactory.create()
    LabelFactory.create(group=group)
    GateAnswerFactory.create(report=report, label_group=group, value=GateAnswer.Value.NO)

    report.body = "changed body"
    report.save()

    client = FakeChatClient(gate_values={group.name: "NO"})
    with _patch_client(client):
        label_report(report.pk)

    assert len(client.gate_calls) == 1  # gate re-asked despite an existing NO answer
    assert client.label_calls == []
```

- [ ] **Step 2: Run the new tests to verify they fail**

Run: `uv run pytest radis/labels/tests/test_labeling.py -k report_update -v`
Expected: both FAIL — `gate_calls` is `[]` because the existing gate answers look fresh.

- [ ] **Step 3: Implement — add the report-side condition to both freshness checks**

In `radis/labels/labeling.py`, the gate condition in `label_report`:

```python
    groups_needing_gate = [
        g
        for g in active_groups
        if g.id not in existing_gates
        or existing_gates[g.id].generated_at < g.updated_at
        or existing_gates[g.id].generated_at < report.updated_at
    ]
```

and `_get_stale_or_missing_labels` (second `.filter()` because the `generated_at__gte` kwarg can't repeat; update the docstring's first line too):

```python
def _get_stale_or_missing_labels(report: Report, labels: list[Label]) -> list[Label]:
    """Return labels whose LabelResult is missing or stale (generated before the label's or
    the report's last update).

    One query answers both "should we run?" (non-empty) and "what to run?" (the list).
    A label that previously came back ABSENT/UNMENTIONED still has a fresh row → excluded.
    """
    fresh_ids = set(
        LabelResult.objects.filter(
            report=report,
            label_id__in=[lbl.id for lbl in labels],
            generated_at__gte=F("label__updated_at"),
        )
        .filter(generated_at__gte=report.updated_at)
        .values_list("label_id", flat=True)
    )
    return [lbl for lbl in labels if lbl.id not in fresh_ids]
```

- [ ] **Step 4: Run the whole labeling test file (skip-path regressions matter here)**

Run: `uv run pytest radis/labels/tests/test_labeling.py radis/labels/tests/test_labeling_integration.py -v`
Expected: all PASS — in particular `test_fresh_gate_and_fresh_results_make_zero_llm_calls` still passes because factories create gates/results after the report, so `generated_at > report.updated_at`.

- [ ] **Step 5: Commit**

```bash
git add radis/labels/labeling.py radis/labels/tests/test_labeling.py
git commit -m "feat(labels): label_report treats results as stale after report update

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 3: Backfill scope picks up updated reports

`_needs_work_queryset` (drives MANUAL backfills) has the same gap: all three freshness predicates ignore `report.updated_at`.

**Files:**
- Modify: `radis/labels/scope.py`
- Test: `radis/labels/tests/test_scope.py` (add two tests)

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: unchanged signature `_needs_work_queryset(active_group_count: int) -> QuerySet`, same two-sided freshness semantics as Task 2.

- [ ] **Step 1: Write the failing tests**

Append to `radis/labels/tests/test_scope.py`:

```python
@pytest.mark.django_db
def test_report_updated_after_gate_needs_work():
    group = LabelGroupFactory.create()
    LabelFactory.create(group=group)
    report = ReportFactory.create()
    GateAnswerFactory.create(report=report, label_group=group, value=GateAnswer.Value.NO)

    report.body = "changed"
    report.save()  # gate now predates report.updated_at -> stale

    assert report.pk in _ids()


@pytest.mark.django_db
def test_report_updated_after_result_needs_work_even_with_refreshed_gate():
    # Isolates the result-side predicate: the gate is re-freshened after the report edit,
    # so only the stale LabelResult can pull the report back into scope.
    from django.utils import timezone

    group = LabelGroupFactory.create()
    label = LabelFactory.create(group=group)
    report = ReportFactory.create()
    gate = GateAnswerFactory.create(report=report, label_group=group, value=GateAnswer.Value.YES)
    LabelResultFactory.create(report=report, label=label, value=LabelResult.Value.PRESENT)

    report.body = "changed"
    report.save()
    # Gate re-answered after the edit (update() bypasses auto_now); result still stale.
    GateAnswer.objects.filter(pk=gate.pk).update(generated_at=timezone.now())

    assert report.pk in _ids()
```

- [ ] **Step 2: Run the new tests to verify they fail**

Run: `uv run pytest radis/labels/tests/test_scope.py -k report_updated -v`
Expected: both FAIL (`report.pk not in _ids()`).

- [ ] **Step 3: Implement — report-side condition in all three predicates**

Replace `_needs_work_queryset` in `radis/labels/scope.py` with:

```python
def _needs_work_queryset(active_group_count: int) -> QuerySet:
    """Reports needing labeling work: missing/stale gate (condition A) OR a fresh YES
    group with a missing/stale label result (condition B). Fresh means generated after
    both the label-side updated_at and the report's updated_at."""
    # Both predicates must match the SAME LabelResult row — otherwise a fresh result on
    # another report could mask this report's stale one.
    fresh_result_for_report = LabelResult.objects.filter(
        report_id=OuterRef(OuterRef("pk")),
        label_id=OuterRef("pk"),
        generated_at__gte=OuterRef("updated_at"),
    ).filter(generated_at__gte=OuterRef(OuterRef("updated_at")))
    fresh_yes_gate = GateAnswer.objects.filter(
        report_id=OuterRef(OuterRef("pk")),
        label_group_id=OuterRef("group_id"),
        value=GateAnswer.Value.YES,
        generated_at__gte=F("label_group__updated_at"),
    ).filter(generated_at__gte=OuterRef(OuterRef("updated_at")))
    return Report.objects.annotate(
        non_stale_gate_count=Count(
            "gate_answers",
            filter=Q(
                gate_answers__label_group__labels__active=True,
                gate_answers__generated_at__gte=F("gate_answers__label_group__updated_at"),
            )
            & Q(gate_answers__generated_at__gte=F("updated_at")),
            distinct=True,
        ),
    ).filter(
        Q(non_stale_gate_count__lt=active_group_count)
        | Exists(
            Label.objects.filter(active=True)
            .filter(Exists(fresh_yes_gate))
            .filter(~Exists(fresh_result_for_report))
        )
    )
```

OuterRef nesting note for the implementer: inside `fresh_result_for_report` / `fresh_yes_gate`, a single `OuterRef` resolves to the enclosing `Label` queryset and a double `OuterRef(OuterRef(...))` to the outer `Report` queryset — that's why the report's `updated_at` needs the double form, and why the extra condition is a chained `.filter()` (the `generated_at__gte` kwarg is already taken). The second `Q` in the `Count` filter is `&`-combined for the same duplicate-kwarg reason.

- [ ] **Step 4: Run the whole scope test file**

Run: `uv run pytest radis/labels/tests/test_scope.py -v`
Expected: all PASS, including the pre-existing masking and distinct-count tests.

- [ ] **Step 5: Commit**

```bash
git add radis/labels/scope.py radis/labels/tests/test_scope.py
git commit -m "feat(labels): backfill scope includes reports updated after labeling

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 4: Model property and admin staleness displays

`LabelResult.is_stale` (model property) and both admin `is_stale` columns must agree with the engine, or operators will see "fresh" rows the scan is about to redo.

**Files:**
- Modify: `radis/labels/models.py:77-80` (`LabelResult.is_stale`)
- Modify: `radis/labels/admin.py:58-60` (`LabelResultAdmin.is_stale`), `radis/labels/admin.py:71-73` (`GateAnswerAdmin.is_stale`)
- Test: `radis/labels/tests/test_models.py` (add one test), `radis/labels/tests/test_admin.py` (add one test)

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `LabelResult.is_stale -> bool` property (two-sided); admin display methods keep their names.

- [ ] **Step 1: Write the failing tests**

Append to `radis/labels/tests/test_models.py` (add any missing imports at the top of the file: `LabelResultFactory` from `radis.labels.factories`):

```python
@pytest.mark.django_db
def test_label_result_is_stale_after_report_update():
    result = LabelResultFactory.create()
    assert not result.is_stale

    result.report.body = "changed"
    result.report.save()
    result.refresh_from_db()

    assert result.is_stale
```

Append to `radis/labels/tests/test_admin.py`:

```python
@pytest.mark.django_db
def test_stale_columns_reflect_report_update():
    from django.contrib import admin as django_admin

    from radis.labels.admin import GateAnswerAdmin, LabelResultAdmin
    from radis.labels.factories import GateAnswerFactory, LabelResultFactory
    from radis.labels.models import GateAnswer, LabelResult

    result = LabelResultFactory.create()
    answer = GateAnswerFactory.create(report=result.report)
    result_admin = LabelResultAdmin(LabelResult, django_admin.site)
    gate_admin = GateAnswerAdmin(GateAnswer, django_admin.site)
    assert not result_admin.is_stale(result)
    assert not gate_admin.is_stale(answer)

    result.report.body = "changed"
    result.report.save()
    result.refresh_from_db()
    answer.refresh_from_db()

    assert result_admin.is_stale(result)
    assert gate_admin.is_stale(answer)
```

- [ ] **Step 2: Run the new tests to verify they fail**

Run: `uv run pytest radis/labels/tests/test_models.py::test_label_result_is_stale_after_report_update radis/labels/tests/test_admin.py::test_stale_columns_reflect_report_update -v`
Expected: both FAIL on the post-update assertions (staleness still ignores the report).

- [ ] **Step 3: Implement**

In `radis/labels/models.py`:

```python
    @property
    def is_stale(self) -> bool:
        """Stale when the label's definition or the report's content changed after generation."""
        return (
            self.generated_at < self.label.updated_at
            or self.generated_at < self.report.updated_at
        )
```

In `radis/labels/admin.py`, `LabelResultAdmin` delegates to the model property; `GateAnswerAdmin` (no model property) gets the inline two-sided check:

```python
    @admin.display(boolean=True, description="Stale")
    def is_stale(self, obj: LabelResult) -> bool:
        return obj.is_stale
```

```python
    @admin.display(boolean=True, description="Stale")
    def is_stale(self, obj: GateAnswer) -> bool:
        return (
            obj.generated_at < obj.label_group.updated_at
            or obj.generated_at < obj.report.updated_at
        )
```

- [ ] **Step 4: Run the affected test files**

Run: `uv run pytest radis/labels/tests/test_models.py radis/labels/tests/test_admin.py radis/labels/tests/test_stale_detection.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add radis/labels/models.py radis/labels/admin.py radis/labels/tests/test_models.py radis/labels/tests/test_admin.py
git commit -m "feat(labels): stale displays account for report updates

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 5: `labels_status` stale counts

The management command's stale counts use only the label-side comparison; they must match the engine's definition.

**Files:**
- Modify: `radis/labels/management/commands/labels_status.py`
- Test: `radis/labels/tests/test_labels_status.py` (add one test)

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: unchanged command name and output format; only the stale numbers change.

- [ ] **Step 1: Write the failing test**

Append to `radis/labels/tests/test_labels_status.py` (extend the imports at the top with `GateAnswerFactory` from `radis.labels.factories`):

```python
@pytest.mark.django_db
def test_labels_status_counts_report_side_staleness(capsys) -> None:
    group = LabelGroupFactory.create(name="Chest")
    label = LabelFactory.create(group=group, name="edema")
    report = ReportFactory.create()
    LabelResultFactory.create(report=report, label=label, value=LabelResult.Value.PRESENT)
    GateAnswerFactory.create(report=report, label_group=group)

    report.body = "changed"
    report.save()  # result and gate now predate report.updated_at

    call_command("labels_status")
    out = capsys.readouterr().out

    assert "1 stale" in out
    assert "0 stale" not in out
```

- [ ] **Step 2: Run the new test to verify it fails**

Run: `uv run pytest radis/labels/tests/test_labels_status.py -k report_side -v`
Expected: FAIL — output shows `0 stale` for both the label and the gate line.

- [ ] **Step 3: Implement — OR in the report comparison**

In `radis/labels/management/commands/labels_status.py`, change the imports line to `from django.db.models import F, Q` and the two stale counts:

```python
            stale = label.results.filter(
                Q(generated_at__lt=F("label__updated_at"))
                | Q(generated_at__lt=F("report__updated_at"))
            ).count()
```

```python
            gstale = group.gate_answers.filter(
                Q(generated_at__lt=F("label_group__updated_at"))
                | Q(generated_at__lt=F("report__updated_at"))
            ).count()
```

- [ ] **Step 4: Run the test file**

Run: `uv run pytest radis/labels/tests/test_labels_status.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add radis/labels/management/commands/labels_status.py radis/labels/tests/test_labels_status.py
git commit -m "feat(labels): labels_status counts report-side staleness

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 6: Docs and full verification

**Files:**
- Modify: `CLAUDE.md` (the "Labels Not Appearing" troubleshooting section)
- Verify: full `radis/labels` test suite + linting

**Interfaces:**
- Consumes: all previous tasks committed.
- Produces: nothing new — this task gates the branch as done.

- [ ] **Step 1: Update CLAUDE.md**

In the "Labels Not Appearing" section, change the second bullet from:

```
- Ensure a backfill has run or the periodic scan (`LABELING_SCAN_CRON`) has ticked since the label/report was created
```

to:

```
- Ensure a backfill has run or the periodic scan (`LABELING_SCAN_CRON`) has ticked since the label was created or the report was created/updated (any report update marks its labels stale and triggers re-labeling)
```

- [ ] **Step 2: Run the full labels test suite**

Run: `uv run pytest radis/labels/tests/ -v`
Expected: all PASS (acceptance tests may be skipped/deselected without dev containers — that's fine; everything that runs must pass).

- [ ] **Step 3: Run linting**

Run: `uv run cli lint`
Expected: clean (ruff + djlint report no errors).

- [ ] **Step 4: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: labeling picks up updated reports, not just new ones

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```
