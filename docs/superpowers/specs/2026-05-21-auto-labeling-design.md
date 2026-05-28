# Auto-Labeling Feature — Design

**Status:** Approved design, ready for implementation planning
**Date:** 2026-05-21 (revised 2026-05-27)
**Owner:** Kai Schlamp

## Overview

Add an auto-labeling feature to RADIS that classifies radiology reports against admin-defined questions using an LLM. Questions are organised into `QuestionGroup`s. Each group carries a **gate question** — a single upfront screening question asked before the group's full question set. The LLM answers every question (gate or regular) with `YES`, `NO`, or `MAYBE`; `YES` and `MAYBE` both attach the label to the report (with `MAYBE` flagged as uncertain). Answers are stored per `(report, question)` pair; gate answers per `(report, question_group)` pair.

Two execution paths share the same labeling logic:

1. **Periodic scan path** — A Procrastinate periodic task runs on a configurable cron schedule. It finds reports created since the last scan tick and enqueues batched labeling tasks. If an admin-triggered backfill is active, the scan advances its checkpoint and yields to the backfill for that tick.
2. **Backfill path** — An admin-triggered `LabelingJob` walks the existing report corpus and produces missing or stale answers. Used for the initial bulk upload and any future large-scale re-labeling (e.g. after a question update). Only one backfill may be active at a time.

Labels surface on the report detail page (badges) and in search (`label:` query filter + facet panel).

## Goals

- Admins can author question groups (with gate questions) and labeling questions in the Django admin and see them applied to reports automatically.
- New and updated reports are labeled in the background without blocking ingest.
- Existing reports (up to ~1M scale) can be backfilled.
- Gating reduces LLM calls by skipping irrelevant question groups entirely.
- Edits to a question or gate question naturally produce stale answers, which the next backfill refreshes.
- End users see applied labels on each report and can filter search by label.

## Non-Goals (v1)

- Manual label editing by end users.
- Versioned answer history (only the latest answer per `(report, question)` is kept).
- Per-question backfill targeting (backfill is system-wide).
- A `label:foo:yes` search syntax that excludes `MAYBE` (revisit if requested).
- Surfacing `NO` answers anywhere user-facing.
- Performance dashboards beyond the live progress shown in the `LabelingJob` admin.
- Localized prompts — questions and reports can be in any language and are passed verbatim to the LLM.

## Data Model

A new Django app `radis.labels` with five models.

```python
class QuestionGroup(models.Model):
    name          = models.CharField(max_length=100, unique=True)
    gate_question = models.TextField()        # upfront screening question for this group
    updated_at    = models.DateTimeField(auto_now=True)  # drives gate stale detection

    class Meta:
        ordering = ["name"]


class Question(models.Model):
    group      = models.ForeignKey(QuestionGroup, on_delete=models.CASCADE,
                                   related_name="questions")
    text       = models.TextField()
    label      = models.CharField(max_length=100)   # label produced when answered YES/MAYBE
    active     = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)  # drives answer stale detection

    class Meta:
        constraints = [models.UniqueConstraint(fields=["label"], name="unique_question_label")]
        indexes     = [models.Index(fields=["active"])]


class Answer(models.Model):
    class Value(models.TextChoices):
        YES   = "YES",   "Yes"
        NO    = "NO",    "No"
        MAYBE = "MAYBE", "Maybe"

    report       = models.ForeignKey(Report, on_delete=models.CASCADE, related_name="answers")
    question     = models.ForeignKey(Question, on_delete=models.CASCADE, related_name="answers")
    value        = models.CharField(max_length=5, choices=Value.choices)
    generated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["report", "question"],
                                    name="unique_answer_per_report_question"),
        ]
        indexes = [
            models.Index(fields=["question", "value"]),  # search facet lookups
            models.Index(fields=["report"]),             # report detail page render
        ]


class GateAnswer(models.Model):
    report         = models.ForeignKey(Report, on_delete=models.CASCADE,
                                       related_name="gate_answers")
    question_group = models.ForeignKey(QuestionGroup, on_delete=models.CASCADE,
                                       related_name="gate_answers")
    value          = models.CharField(max_length=5, choices=Answer.Value.choices)
    generated_at   = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["report", "question_group"],
                                    name="unique_gate_answer_per_report_group"),
        ]
        indexes = [
            models.Index(fields=["question_group", "value"]),
        ]


class LabelingScanCheckpoint(models.Model):
    last_scanned_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = "Labeling scan checkpoint"
        constraints = [
            models.CheckConstraint(check=models.Q(id=1),
                                   name="singleton_labeling_scan_checkpoint"),
        ]

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)
```

### Stale detection

- **Answer stale:** `Answer.generated_at < F("question__updated_at")`
- **Gate answer stale:** `GateAnswer.generated_at < F("question_group__updated_at")`

No snapshot columns, no `is_stale` flags. Both comparisons are pure joins over indexed columns.

### Constraints

- `QuestionGroup.name` is unique. Groups are the unit of gate authoring.
- `Question.label` is unique. Two questions sharing a label would be ambiguous.
- `(Answer.report, Answer.question)` is unique. Re-labeling replaces via `update_or_create`.
- `(GateAnswer.report, GateAnswer.question_group)` is unique. Re-evaluation replaces via `update_or_create`.
- Cascade deletes on all FKs. Deleting a `QuestionGroup` cascades to its `Question`s and `GateAnswer`s; deleting a `Question` cascades to its `Answer`s. Admins are warned by Django's standard delete confirmation.

## Execution Path 1: Periodic Incremental Scan

Replaces the original signal-based per-report path. RADIS controls batching entirely — no dependency on the shape or cadence of any external ETL pipeline.

### Design rationale

The signal-based approach tied task granularity to the ETL pipeline's call size: a bulk ingest of 1,000 reports would produce 1,000 individual queue tasks. The periodic scan decouples these concerns: reports accumulate in the database via normal ingest, and RADIS labels them on its own schedule in controlled batches.

The scan's responsibility is narrow: label newly ingested reports. Finding reports with missing or stale answers (e.g. after a question edit) is exclusively the backfill's job.

### Trigger

A Procrastinate periodic task registered with `@app.periodic(cron=settings.LABELING_SCAN_CRON)`.

### Checkpoint

`LabelingScanCheckpoint` (single row, `pk=1`) tracks `last_scanned_at`. The scan queries `Report.objects.filter(created_at__gte=last_scanned_at)` and advances the checkpoint to the current tick's timestamp at the end of every tick — including ticks that are skipped due to an active backfill.

On the very first tick (`last_scanned_at is None`), the scan records the current timestamp and exits without enqueuing anything. Existing reports at that point must be handled by the backfill.

### Guard: yield to active backfill

When an admin-triggered backfill is running, the scan returns without enqueuing and **does not advance the checkpoint**. Once the backfill completes, the scan resumes from the pre-backfill checkpoint and queries all reports created since that point. Reports already labeled by the backfill are encountered but cost nothing — `label_report` finds their gate answers and regular answers fresh and exits immediately with zero LLM calls. Reports created after the backfill's cursor snapshot (the gap) are labeled for the first time. The checkpoint then advances to the current tick time.

### Batch and enqueue

```python
@app.periodic(cron=settings.LABELING_SCAN_CRON)
@app.task()
def incremental_label_scan(timestamp: int) -> None:
    now = datetime.fromtimestamp(timestamp, tz=timezone.utc)
    checkpoint, _ = LabelingScanCheckpoint.objects.get_or_create(pk=1)

    if LabelingJob.objects.filter(status__in=LabelingJob.ACTIVE_STATUSES).exists():
        logger.info("Active LabelingJob found, skipping scan tick (checkpoint unchanged).")
        return

    if checkpoint.last_scanned_at is None:
        # First run: record this moment; existing reports belong to the backfill.
        checkpoint.last_scanned_at = now
        checkpoint.save()
        return

    if not Question.objects.filter(active=True).exists():
        return

    report_ids = (
        Report.objects.filter(created_at__gte=checkpoint.last_scanned_at)
        .order_by("pk")
        .values_list("id", flat=True)
        .iterator(chunk_size=1000)
    )

    for batch in batched(report_ids, settings.LABELING_TASK_BATCH_SIZE):
        label_report_batch.defer_with_priority(
            priority=settings.LABELING_PER_REPORT_PRIORITY,
            report_ids=list(batch),
        )

    checkpoint.last_scanned_at = now
    checkpoint.save()
```

### Task

```python
@app.task(queue="llm")
def label_report_batch(report_ids: list[int]) -> None:
    success = failure = 0
    for report_id in report_ids:
        try:
            label_report(report_id)
            success += 1
        except Exception:
            logger.exception("Labeling failed for report %d", report_id)
            failure += 1
    logger.info(
        "Batch complete: %d success, %d failure out of %d",
        success, failure, len(report_ids),
    )
```

One task per batch of `LABELING_TASK_BATCH_SIZE` reports (default 100). Calls `label_report()` sequentially — the same core function used by the backfill processor. Per-report exceptions are caught so one failing report does not abort the rest of the batch; failed reports are left for the next backfill to recover via the missing-answers predicate.

### Scale

At ~100 reports/day and default batch size 100, each tick produces at most one queue task. No task explosion tied to ETL call size.

## Execution Path 2: Backfill

Mirrors the `extractions` app's Job/Task pattern.

### Models

```python
class LabelingJob(AnalysisJob):
    default_priority = settings.LABELING_BACKFILL_PRIORITY
    urgent_priority  = settings.LABELING_BACKFILL_PRIORITY  # backfill is never urgent

    # Singleton constraint: at most one LabelingJob may be in an active status at any time.
    # Implemented as a partial unique index in the migration.
    ACTIVE_STATUSES = (
        AnalysisJob.Status.UNVERIFIED,
        AnalysisJob.Status.PREPARING,
        AnalysisJob.Status.PENDING,
        AnalysisJob.Status.IN_PROGRESS,
        AnalysisJob.Status.CANCELING,
    )

    def delay(self) -> None:
        queued_job_id = app.configure_task(
            "radis.labels.tasks.process_labeling_job",
            allow_unknown=False,
            priority=self.default_priority,
        ).defer(job_id=self.pk)
        self.queued_job_id = queued_job_id
        self.save()


class LabelingTask(AnalysisTask):
    job     = models.ForeignKey(LabelingJob, on_delete=models.CASCADE, related_name="tasks")
    reports = models.ManyToManyField(Report, related_name="+")
```

### Phase 1 — PREPARING (default queue)

```python
@app.task()
def process_labeling_job(job_id: int) -> None:
    job = LabelingJob.objects.get(id=job_id)
    job.tasks.all().delete()  # wipe partial rows from any prior crashed attempt
    job.status = AnalysisJob.Status.PREPARING
    job.started_at = timezone.now()
    job.save()

    create_labeling_tasks_streaming(job)
    job.status = AnalysisJob.Status.PENDING
    job.save()

    enqueue_all_pending_tasks(job)
```

`create_labeling_tasks_streaming` iterates `Report.objects.order_by("pk").iterator(chunk_size=1000)`. For each chunk it identifies reports needing work using `_needs_work_queryset` and bulk-creates `LabelingTask` rows of `LABELING_TASK_BATCH_SIZE` reports each.

Django's `iterator(chunk_size=...)` opens a PostgreSQL server-side cursor, so the result set is frozen at the moment the cursor opens. Reports ingested after that point are invisible to the backfill — they are not added to its task list and do not extend its runtime. The periodic scan's checkpoint is held frozen while the backfill runs (the guard returns without advancing it); once the backfill completes, the scan resumes from the pre-backfill checkpoint and covers the full window including reports the backfill's cursor never saw.

The `tasks.all().delete()` at the top makes the preparation phase idempotent. If Procrastinate retries the task after a crash mid-streaming, any partial `LabelingTask` rows from the previous attempt are cleared before streaming restarts. The cost is bounded — only this job's own tasks are deleted, however many the prior attempt managed to create.

### "Needs work" predicate

Used exclusively by the backfill. A report needs work if **either** condition holds:

```python
def _needs_work_queryset(
    active_question_count: int,
    active_group_count: int,
) -> QuerySet:
    return (
        Report.objects.annotate(
            non_stale_answer_count=Count(
                "answers",
                filter=Q(
                    answers__question__active=True,
                    answers__generated_at__gte=F("answers__question__updated_at"),
                ),
            ),
            non_stale_gate_count=Count(
                "gate_answers",
                filter=Q(
                    gate_answers__question_group__questions__active=True,
                    gate_answers__generated_at__gte=F(
                        "gate_answers__question_group__updated_at"
                    ),
                ),
                distinct=True,
            ),
        )
        .filter(
            Q(non_stale_answer_count__lt=active_question_count)
            | Q(non_stale_gate_count__lt=active_group_count)
        )
    )
```

- **Condition 1** — missing or stale regular answers (question edited, or never labeled).
- **Condition 2** — missing or stale gate answers (gate question edited, or group never gated).

### Phase 2 — IN_PROGRESS (`llm` queue)

```python
@app.task(queue="llm")
def process_labeling_task(task_id: int) -> None:
    LabelingTaskProcessor(LabelingTask.objects.get(id=task_id)).start()
```

`LabelingTaskProcessor` subclasses `AnalysisTaskProcessor` and overrides `process_task`. For each report in the task, it calls `label_report(report_id)`. Within the task it uses a `ThreadPoolExecutor(max_workers=LABELING_LLM_CONCURRENCY_LIMIT)`, mirroring extractions.

### Cancellation and resumability

- **Cancel:** admin clicks Cancel → status flips to `CANCELING`. The base processor marks tasks `CANCELED`.
- **Resume:** the next backfill recomputes its scope from current state. Non-stale pairs are skipped. Backfill is idempotent and safe to start, cancel, and restart.
- **Procrastinate retry after crash:** if `process_labeling_job` raises before completing, Procrastinate retries it. The `tasks.all().delete()` at the top ensures any partial `LabelingTask` rows from the failed attempt are wiped before the preparation phase reruns.

### Scale estimate

With defaults (`concurrency=6`, `groups_per_report≈3`, `seconds_per_call≈3 s`): ≈ 0.67 reports/sec without gating. Gating reduces effective LLM calls per report significantly when most groups screen negative, improving throughput proportionally.

## Core Labeling Function

`label_report` is the single function used by both execution paths. It encapsulates the two-phase gate-then-label logic.

```python
def label_report(report_id: int) -> None:
    report = Report.objects.get(id=report_id)
    if not report.body or not report.body.strip():
        return

    active_groups = (
        QuestionGroup.objects
        .filter(questions__active=True)
        .prefetch_related("questions")
        .distinct()
    )
    if not active_groups:
        return

    client = ChatClient()

    # Load existing gate answers to classify groups by gate status
    existing_gates = {
        ga.question_group_id: ga
        for ga in GateAnswer.objects.filter(report=report, question_group__in=active_groups)
    }

    groups_needing_gate = [
        g for g in active_groups
        if g.id not in existing_gates
        or existing_gates[g.id].generated_at < g.updated_at
    ]
    groups_with_fresh_gate = {
        g.id: existing_gates[g.id].value
        for g in active_groups
        if g.id not in [ng.id for ng in groups_needing_gate]
    }

    # Phase 1 — Gate: only for groups with stale or missing gate answers
    new_gate_results: dict[int, str] = {}
    for gate_batch in batched(groups_needing_gate, settings.LABELING_GATE_BATCH_SIZE):
        schema = build_gate_schema(gate_batch)
        prompt = render_gate_prompt(report.body, gate_batch)
        parsed = client.extract_data(prompt, schema)
        new_gate_results.update(parsed)

    # Phase 2 — Process each group
    for group in active_groups:
        questions = [q for q in group.questions.all() if q.active]

        if group.id in new_gate_results:
            # Gate was re-evaluated this run
            new_value = new_gate_results[group.id]
            old_gate  = existing_gates.get(group.id)
            old_value = old_gate.value if old_gate else None

            GateAnswer.objects.update_or_create(
                report=report, question_group=group,
                defaults={"value": new_value},
            )

            if new_value in (Answer.Value.YES, Answer.Value.MAYBE):
                prev_was_negative = old_value in (Answer.Value.NO, None)
                if prev_was_negative or _has_missing_or_stale_answers(report, group):
                    _run_question_set(client, report, questions)
                # else: gate still YES/MAYBE and answers fresh — nothing to do
            else:
                _write_synthetic_nos(report, questions)

        else:
            # Gate is fresh — use stored value, no gate LLM call
            gate_value = groups_with_fresh_gate[group.id]

            if gate_value in (Answer.Value.YES, Answer.Value.MAYBE):
                if _has_missing_or_stale_answers(report, group):
                    _run_question_set(client, report, questions)
            else:
                # Gate = NO, fresh. Re-write synthetic NOs to bump generated_at
                # and clear any stale answer flags caused by question text edits.
                _write_synthetic_nos(report, questions)
```

### Decision table

| Gate was | Gate is now | Regular answers | Action |
|---|---|---|---|
| YES/MAYBE | YES/MAYBE | Non-stale | Save gate answer |
| YES/MAYBE | YES/MAYBE | Stale/missing | Save gate answer + run question set |
| YES/MAYBE | NO | Any | Save gate answer + write synthetic NOs |
| NO | YES/MAYBE | Any | Save gate answer + run question set |
| NO | NO | Any | Save gate answer + re-write synthetic NOs (bump `generated_at`) |
| missing | YES/MAYBE | Any | Save gate answer + run question set |
| missing | NO | Any | Save gate answer + write synthetic NOs |

"Gate is now" represents the LLM's re-evaluation result — these rows apply when the gate was stale or missing. `GateAnswer.update_or_create` is always called, which bumps `generated_at` even when the value is unchanged. When the gate is fresh (not re-evaluated), the gate answer is not saved; if the stored value is YES/MAYBE and answers are fresh, no action is taken.

### LLM call count (20 groups, gate batch 10, 3 pass gate)

| Scenario | Without gating | With gating |
|---|---|---|
| First labeling | 20 | 2 gate + 3 group = **5** |
| Re-run, 1 question stale (gate fresh) | 20 | 0 gate + 1 group = **1** |
| Re-run, gate text changed (still YES) | 20 | 2 gate + 0 group = **2** |
| Re-run, gate flips NO→YES | 20 | 2 gate + 3 group = **5** |

### Skip conditions

- `report.body` empty/whitespace.
- No active questions / no active groups.

### Failure handling

Per-report exceptions are caught inside `label_report_batch`; one report's LLM failure does not abort the rest of the batch. Failures are logged at `ERROR` level with full traceback. A report that fails in the scan will not be re-attempted by the scan (the checkpoint will have advanced past its `created_at`); the next admin-triggered backfill will catch it via the missing-answers predicate. Procrastinate retries the batch task itself only on uncaught exceptions (i.e. infrastructure failures outside the per-report loop).

## Admin UX

### `QuestionGroupAdmin`

Primary surface for authoring groups and their gate questions.

```python
class QuestionGroupAdmin(admin.ModelAdmin):
    list_display    = ("name", "gate_question_preview", "active_question_count", "updated_at")
    search_fields   = ("name",)   # required for autocomplete in QuestionAdmin
    ordering        = ("name",)
    readonly_fields = ("updated_at", "gate_answer_summary")
    fieldsets = (
        (None,    {"fields": ("name", "gate_question")}),
        ("Stats", {"fields": ("gate_answer_summary", "updated_at")}),
    )
```

- `gate_question_preview` truncates to ~80 chars in list view.
- `gate_answer_summary` shows live gate counts: `12,345 Yes · 3,210 Maybe · 44,500 No · 87 stale`.
- `updated_at` is read-only — bumps automatically on any save, driving gate staleness.
- Admins must create a `QuestionGroup` before adding questions to it; the gate question is a required part of the group.

### `QuestionAdmin`

`group` changes from a free-text `datalist` input to a proper FK autocomplete:

```python
class QuestionAdmin(admin.ModelAdmin):
    autocomplete_fields = ["group"]
    list_display        = ("label", "group", "active", "text_preview", "updated_at", "answer_summary")
    list_filter         = ("active", "group")
    search_fields       = ("label", "group__name", "text")
    ordering            = ("group__name", "label")
    readonly_fields     = ("created_at", "updated_at", "answer_summary")
    fieldsets = (
        (None,    {"fields": ("group", "label", "text", "active")}),
        ("Stats", {"fields": ("answer_summary", "created_at", "updated_at")}),
    )
```

### `LabelingScanCheckpointAdmin`

Read-only ops view showing the current checkpoint timestamp:

```python
class LabelingScanCheckpointAdmin(admin.ModelAdmin):
    list_display    = ("last_scanned_at",)
    readonly_fields = ("last_scanned_at",)

    def has_add_permission(self, request):    return False
    def has_change_permission(self, *a):      return False
    def has_delete_permission(self, *a):      return False
```

### `LabelingJobAdmin`

Unchanged from original spec. The backfill cockpit with Run/Cancel banner and live progress panel.

### `AnswerAdmin`

Unchanged from original spec. Read-only ops/debugging view.

### `GateAnswerAdmin`

Read-only, mirrors `AnswerAdmin`:

```python
class GateAnswerAdmin(admin.ModelAdmin):
    list_display    = ("report", "question_group", "value", "is_stale", "generated_at")
    list_filter     = ("value", "question_group")
    search_fields   = ("report__document_id", "question_group__name")
    raw_id_fields   = ("report", "question_group")
    readonly_fields = tuple(f.name for f in GateAnswer._meta.fields)

    def has_add_permission(self, request):    return False
    def has_change_permission(self, *a):      return False
```

`is_stale` annotated via `F("question_group__updated_at") > F("generated_at")`.

### `ReportAdmin` inline

Unchanged — `AnswerInline` on the Report change form shows per-question answers.

### Intentionally omitted (v1)

- "Preview LLM call" button on `QuestionGroupAdmin`.
- Bulk "mark stale" action.
- Per-question backfill.

## End-User Surfacing

### Report detail page

Unchanged from original spec. Cotton component `<c-report-labels :report="report" />` renders YES/MAYBE badges grouped by `question_group.name`; NO answers not shown; stale answers shown with muted style and tooltip.

### Search filter (`label:`)

Unchanged from original spec. `label:pneumonia` filters by `question.label` and `value IN (YES, MAYBE)`. Facet panel lists top N labels with counts.

## Settings

Added to `radis/settings/base.py`:

```python
# Priorities (Procrastinate uses higher = sooner).
# Existing baseline:
#   EXTRACTION_DEFAULT_PRIORITY = 2 · EXTRACTION_URGENT_PRIORITY = 3
#   SUBSCRIPTION_DEFAULT_PRIORITY = 3 · SUBSCRIPTION_URGENT_PRIORITY = 4
LABELING_PER_REPORT_PRIORITY = env.int("LABELING_PER_REPORT_PRIORITY", default=1)
LABELING_BACKFILL_PRIORITY   = env.int("LABELING_BACKFILL_PRIORITY",   default=0)

LABELING_TASK_BATCH_SIZE       = env.int("LABELING_TASK_BATCH_SIZE",       default=100)
LABELING_LLM_CONCURRENCY_LIMIT = env.int("LABELING_LLM_CONCURRENCY_LIMIT", default=6)
LABELING_GATE_BATCH_SIZE       = env.int("LABELING_GATE_BATCH_SIZE",       default=10)

# Cron schedule for the periodic incremental scan (default: every 15 minutes).
LABELING_SCAN_CRON = env.str("LABELING_SCAN_CRON", default="*/15 * * * *")

LABELING_SYSTEM_PROMPT      = env.str("LABELING_SYSTEM_PROMPT",      default=DEFAULT_LABELING_SYSTEM_PROMPT)
LABELING_GATE_SYSTEM_PROMPT = env.str("LABELING_GATE_SYSTEM_PROMPT", default=DEFAULT_GATE_SYSTEM_PROMPT)
```

Default gate prompt:

```text
You are an AI medical assistant. For each topic below, answer whether the radiology
report contains content relevant to that topic.

For each topic, respond with exactly one of:
  - "YES"   — the report clearly contains relevant content
  - "NO"    — the report clearly does not
  - "MAYBE" — the report may contain relevant content; use when uncertain

Return answers in JSON format matching the provided schema.

Radiology Report:
$report

Topics:
$gate_questions
```

## Operational Considerations

### Worker / queue topology

No new queues or containers. Priority table unchanged:

| Source | Priority |
|---|---|
| Urgent subscriptions | 4 |
| Urgent extractions / default subs | 3 |
| Default extractions | 2 |
| Incremental scan batches | 1 |
| Backfill labeling | 0 |

### Database growth

- `Answer` table: reports × active questions (1M × 20 ≈ 20M rows).
- `GateAnswer` table: reports × active groups (1M × 5 ≈ 5M rows, assuming ~4 questions per group averages to ~5 groups).
- Indexes declared on initial migrations (built while tables are empty).

### Logging

`radis.labels` logger:
- `INFO` on batch task start/finish (batch size + duration).
- `INFO` on gate phase: groups evaluated, groups skipped (gate fresh).
- `WARNING` on skip (empty body, no active questions).
- `ERROR` with full traceback on LLM failure after retries.

### Observability

- Live progress in `LabelingJobAdmin` change form.
- Management command `uv run cli labels-status`: last scan checkpoint timestamp (or "never"), total reports, fully-current count, missing/stale count, per-question Yes/Maybe/No/stale counts, per-group gate Yes/Maybe/No/stale counts.
- No Prometheus metrics in v1.

### Documentation updates

- `CLAUDE.md`: add `radis.labels` to Django Apps list; add the eight new env vars; add Troubleshooting subsection.
- `KNOWLEDGE.md`: document prompt design, gate question authoring guidelines (gate question should be answerable from the report body; phrase as a topic-level screen, not a specific finding).
- `example.env`: eight new env vars with comments and defaults.

### Existing-system touchpoints

1. `radis/reports/admin.py` — add `AnswerInline` to `ReportAdmin.inlines`.
2. `radis/search/parser.py` — register `label:` as a known field filter.
3. `radis/pgsearch/` — translator for the `label:` AST node.
4. `radis/settings/base.py` — new settings block (additive).
5. Templates — Cotton component on report detail and search facet panel.

No touch to `radis/reports/site.py` — the periodic scan does not use the reports created/updated handler extension points.

## Testing Strategy

### Unit tests (no DB)

- **Prompt rendering** (`test_prompts.py`) — snapshot tests of `render_questions_prompt` and `render_gate_prompt` for representative inputs and unicode content.
- **Schema building** (`test_schemas.py`) — `build_yes_no_maybe_schema` and `build_gate_schema` produce Pydantic models that validate correct payloads and reject unknown/missing fields.

### Model / DB tests

- **Stale detection** (`test_stale_detection.py`) — assert answer and gate-answer stale predicates over hand-built rows.
- **Backfill scope query** (`test_scope.py`) — reports covering all combinations of answer/gate-answer freshness; the scope query returns exactly those needing work.
- **Singleton backfill** (`test_singleton.py`) — second concurrent active `LabelingJob` raises `IntegrityError`.
- **Backfill restart safety** (`test_singleton.py`) — if `process_labeling_job` is called twice for the same job (simulating a Procrastinate retry), partial `LabelingTask` rows from the first call are deleted before the second call recreates them; no duplicates result.
- **Cascade and uniqueness** (`test_models.py`) — group delete cascades to questions and gate answers; question delete cascades to answers; unique constraints enforced; upserts work.

### Integration tests (LLM mocked)

- **Gate batching** (`test_gate.py`) — 20 groups with gate batch size 10 → exactly 2 gate LLM calls; only YES/MAYBE groups produce question-set calls.
- **Synthetic NOs** (`test_gate.py`) — NO-gated groups produce synthetic `Answer(value=NO)` rows with no LLM call.
- **Gate fresh + YES/MAYBE + answers fresh** → zero LLM calls.
- **Gate fresh + YES/MAYBE + 1 question stale** → exactly 1 question-set call, 0 gate calls.
- **Gate stale + new YES, old NO** → 1 gate call + 1 question-set call (synthetic → real).
- **Gate stale + new YES, old YES + answers fresh** → 1 gate call only, 0 question-set calls.
- **Gate stale + new NO** → 1 gate call, synthetic NOs written, no question-set call.
- **Stale answer in NO-gated group** → synthetic NOs re-written (generated_at bumped), no LLM call.
- **Skip conditions** (`test_skips.py`) — empty body / no active questions → no LLM call.
- **Backfill task processor** (`test_processor.py`) — partial failure on one report yields task `WARNING`; successful reports still have answers written.
- **Incremental scan: first run** (`test_scan.py`) — null checkpoint → no tasks deferred, checkpoint created and set to `now`.
- **Incremental scan: guard** (`test_scan.py`) — active `LabelingJob` → no tasks deferred, checkpoint unchanged.
- **Incremental scan: batching** (`test_scan.py`) — 250 reports with `created_at` after the checkpoint → 3 batch tasks deferred (100, 100, 50); reports with `created_at` before the checkpoint produce zero tasks.
- **Incremental scan: checkpoint singleton** (`test_scan.py`) — calling `save()` twice on separate `LabelingScanCheckpoint` instances does not create a second row; `has_add_permission` returns `False` in admin.
- **Incremental scan: partial batch failure** (`test_scan.py`) — one report raises during `label_report`; the remaining reports in the batch are still labeled and the task itself does not raise.

### Admin tests

- **`QuestionGroupAdmin`** — duplicate name surfaces as form error; `updated_at` bumps on save.
- **`QuestionAdmin`** — autocomplete on group FK works; duplicate label surfaces as form error.
- **`LabelingJobAdmin`** — Run/conflict/Cancel flows unchanged.

### Search integration tests

Unchanged from original spec.

### UI tests (Playwright, `@pytest.mark.acceptance`)

Unchanged from original spec.

### One real-LLM acceptance test (`@pytest.mark.acceptance`, `cpu` profile)

- Create one group ("Lung findings", gate: "Does this report contain lung-related findings?"), one question ("Is the chest clear?"), ingest a synthetic report stating "No abnormalities, lungs clear.", trigger `incremental_label_scan` directly, assert a `GateAnswer(value=YES)` and `Answer(value=YES)` appear.

### Out of scope (test-wise)

- Performance / load tests on 1M-row backfill.
- Procrastinate's retry semantics.
- Migrations on existing data — all tables start empty.

## Risks and Open Questions

- **LLM cost on the 1M-report backfill.** Gating reduces per-report cost significantly, but a full backfill is still a multi-day operation. Cost should be modeled before turning on for production.
- **Gate question wording quality.** A poorly worded gate question can screen out reports incorrectly. Admins are expected to author gate questions carefully; no safeguards in v1.
- **`label:` token in user queries.** If free-text search contains `label:`, the parser treats it as a field filter. Matches behavior of other field filters; acceptable.
- **Report deletion blast radius.** Cascade to `Answer` and `GateAnswer` rows. Standard Django behavior.

## Out of Scope (Future Work)

- Manual override / user-correction of labels.
- Versioned answer history.
- Per-question or per-group backfill triggered from admin.
- A "stale" search filter modifier.
- LLM call preview in the question group admin.
- Prometheus metrics on labeling throughput.
- Localized prompts.
