# Auto-Labeling Feature — Design

**Status:** Approved design, ready for implementation planning
**Date:** 2026-05-21
**Owner:** Kai Schlamp

## Overview

Add an auto-labeling feature to RADIS that classifies radiology reports against admin-defined questions using an LLM. Each question carries the label it produces and a group string used to batch related questions into a single LLM call. The LLM answers each question with `YES`, `NO`, or `MAYBE`; `YES` and `MAYBE` both attach the label to the report (with `MAYBE` flagged as uncertain). Answers are stored per `(report, question)` pair.

Two execution paths share the same labeling logic:

1. **Ingest path** — Reports are created or updated by ETL pipelines (never one-at-a-time interactively). The reports app calls registered handlers with a `list[Report]` per ingest batch. The labels handler chunks the batch and enqueues one background `label_report_batch` task per chunk; each task labels its members in parallel against all active questions.
2. **Backfill path** — An admin-triggered `LabelingJob` walks the existing report corpus and produces missing or stale answers. Only one backfill may be active at a time.

Labels surface on the report detail page (badges) and in search (multi-select filter + facet panel).

## Goals

- Admins can author labeling questions in the Django admin and see them applied to reports automatically.
- ETL-ingested reports (new and updated) are labeled in the background without blocking ingest, and at a queue size proportional to ingest batches — not to the number of reports.
- Existing reports (up to ~1.5M scale) can be backfilled.
- Edits to a question naturally produce stale answers, which the next backfill refreshes.
- End users see applied labels on each report and can filter search by label.

## Non-Goals (v1)

- Manual label editing by end users.
- Versioned answer history (only the latest answer per `(report, question)` is kept).
- Per-question backfill targeting (backfill is system-wide).
- A free-text query syntax for labels (e.g., `label:foo` operators in the search box). v1 uses a SearchFilters multi-select control instead, matching how existing filters (`modalities`, `language`, etc.) are exposed today. Adding a QueryParser-level `label:` token is a possible follow-up.
- A YES-only filter modifier that excludes `MAYBE` results (revisit if requested).
- Surfacing `NO` answers anywhere user-facing.
- Performance dashboards beyond the live progress shown in the `LabelingJob` admin.
- Localized prompts — questions and reports can be in any language and are passed verbatim to the LLM.

## Data Model

A new Django app `radis.labels` with two models. Both are denormalized: the question carries its label name and group string directly, no `Label` or `QuestionGroup` table.

```python
class Question(models.Model):
    text       = models.TextField()                  # the natural-language question
    label      = models.CharField(max_length=100)    # the label produced when answered YES/MAYBE
    group      = models.CharField(max_length=100)    # batching key for the LLM call
    active     = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True) # drives stale detection on answers

    class Meta:
        indexes = [models.Index(fields=["active", "group"])]
        constraints = [models.UniqueConstraint(fields=["label"], name="unique_question_label")]


class Answer(models.Model):
    class Value(models.TextChoices):
        YES   = "YES",   "Yes"
        NO    = "NO",    "No"
        MAYBE = "MAYBE", "Maybe"

    report       = models.ForeignKey(Report, on_delete=models.CASCADE, related_name="answers")
    question     = models.ForeignKey(Question, on_delete=models.CASCADE, related_name="answers")
    value        = models.CharField(max_length=5, choices=Value.choices)
    generated_at = models.DateTimeField(auto_now=True)   # bumped on every (re)write

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["report", "question"], name="unique_answer_per_report_question"),
        ]
        indexes = [
            models.Index(fields=["question", "value"]),  # search facet lookups
            models.Index(fields=["report"]),             # report detail page render
        ]
```

### Stale detection

An answer is stale when its question has been edited since the answer was generated:

```python
stale = Answer.objects.filter(generated_at__lt=F("question__updated_at"))
```

No snapshot column, no `is_stale` flag, no row updates when a question is edited. The comparison is a pure join over indexed columns.

### Constraints

- `Question.label` is unique. Two questions sharing a label would be ambiguous.
- `(Answer.report, Answer.question)` is unique. Re-labeling replaces the existing row via `update_or_create`.
- Cascade deletes on both FKs. Deleting a question with many answers is heavy but acceptable; admins are warned by Django's standard delete confirmation.

## Execution Path 1: Ingest Batch Labeling

Driven by the existing reports app handlers, which fire per ingest batch (not per row). Used for both newly-ingested and updated reports. Reports enter RADIS only through ETL pipelines or admin actions — there is no interactive per-row creation flow to optimize for — so the design routes every handler call through the same batch path.

### Trigger

Register handlers using the existing extension points in `radis.reports.site`. The handler signature receives a list of reports, matching the `Callable[[list[Report]], None]` type already required by the reports app:

```python
from radis.reports.site import (
    ReportsCreatedHandler,
    ReportsUpdatedHandler,
    register_reports_created_handler,
    register_reports_updated_handler,
)

register_reports_created_handler(
    ReportsCreatedHandler(name="labels", handle=_label_reports_handler)
)
register_reports_updated_handler(
    ReportsUpdatedHandler(name="labels", handle=_label_reports_handler)
)
```

The handler chunks the incoming list into sub-batches of at most `LABELING_TASK_BATCH_SIZE` (default 100, the same constant used by the backfill) and defers one Procrastinate task per chunk. This caps any single task's runtime and limits retry blast radius if one chunk fails.

```python
def _label_reports_handler(reports: list[Report]) -> None:
    deferrer = app.configure_task(
        "radis.labels.tasks.label_report_batch",
        allow_unknown=False,
        priority=settings.LABELING_INGEST_PRIORITY,
    )
    report_ids = [r.id for r in reports]
    for chunk in _chunked(report_ids, settings.LABELING_TASK_BATCH_SIZE):
        deferrer.defer(report_ids=chunk)
```

### Task

```python
@app.task(queue="llm")
def label_report_batch(report_ids: list[int]) -> None:
    label_reports_in_parallel(report_ids)
```

Configured with priority `LABELING_INGEST_PRIORITY` (default `1`) — higher than backfill (`0`) so newly-ingested reports get labeled before backfill catches up, but lower than urgent extractions/subscriptions so user-initiated work isn't delayed.

### Shared parallel-labeling helper

The same helper is used by both the ingest batch task and the backfill `LabelingTaskProcessor`. Single source of truth for "label N reports concurrently":

```python
def label_reports_in_parallel(
    report_ids: list[int], client: ChatClient | None = None
) -> tuple[int, int]:
    """Return (success_count, failure_count). Logs failures."""
    chat = client or ChatClient()
    success = failure = 0
    with ThreadPoolExecutor(
        max_workers=settings.LABELING_LLM_CONCURRENCY_LIMIT
    ) as executor:
        futures = [executor.submit(label_report, rid, chat) for rid in report_ids]
        for f in futures:
            try:
                f.result()
                success += 1
            except Exception as exc:  # noqa: BLE001 — log and continue.
                logger.exception("Labeling failed for one report: %s", exc)
                failure += 1
    return success, failure
```

### Core single-report function

Unchanged. Used by `label_reports_in_parallel` for each member of the batch:

```python
def label_report(report_id: int, client: ChatClient | None = None) -> None:
    report = Report.objects.get(id=report_id)
    if not report.body or not report.body.strip():
        return
    questions_by_group = group_active_questions_by_group()
    if not questions_by_group:
        return

    existing = {
        a.question_id: a
        for a in Answer.objects.filter(report=report).select_related("question")
    }
    chat = client or ChatClient()
    for group_str, questions in questions_by_group.items():
        if _group_answers_are_current(questions, existing, report.updated_at):
            continue  # skip the LLM call for this group — every answer is current
        Schema = build_yes_no_maybe_schema(questions)
        prompt = render_questions_prompt(report.body, questions)
        parsed = chat.extract_data(prompt, Schema)
        upsert_answers(report, questions, parsed.model_dump())
```

One LLM call per question group per report. `upsert_answers` uses `update_or_create` per `(report, question)`. `generated_at` is bumped on every write via `auto_now=True`.

### Per-group idempotency

To avoid redundant LLM calls when only a subset of questions has changed (the canonical case: an admin edits one question, the admin then runs a backfill — every report has one stale answer, but most groups remain fully current), `label_report` checks each group before calling the LLM. A group is skipped iff **every** question in the group has an existing answer satisfying both conditions:

```text
answer.generated_at >= question.updated_at   AND
answer.generated_at >= report.updated_at
```

The first clause means "the answer was generated after the question was last edited" — i.e. the answer reflects the current question text. The second clause means "the answer was generated after the report was last modified" — guarding against amended report bodies. If either clause fails for any question in the group, the LLM is called for the whole group and `upsert_answers` rewrites all answers in that group.

```python
def _group_answers_are_current(
    questions: list[Question],
    existing: dict[int, Answer],
    report_updated_at: datetime,
) -> bool:
    for q in questions:
        a = existing.get(q.id)
        if a is None:
            return False
        if a.generated_at < q.updated_at:
            return False
        if a.generated_at < report_updated_at:
            return False
    return True
```

This is the **second layer of efficiency** in the system. The first layer is `find_reports_needing_work`, which scopes a backfill to reports with at least one missing or stale answer (avoiding work on fully-current reports entirely). The second layer is per-group skip inside `label_report` (avoiding LLM calls on already-current groups within reports that *do* need work). Both layers compose: a backfill triggered by a single question edit only touches reports that need work AND only makes LLM calls for groups that include the changed question.

**Intentional semantics — any report upload triggers re-labeling.** Every Report write path bumps `report.updated_at`: `auto_now=True` on the model handles ORM/admin/single-report-API writes, and the bulk upsert (`_bulk_upsert_reports` in `radis/reports/api/viewsets.py`) explicitly sets `existing.updated_at = now` and includes it in the `bulk_update` field list. There is no path that updates a Report row without bumping `updated_at`. Therefore the per-group check's `answer.generated_at >= report.updated_at` clause **always fails on the ingest path** — re-uploaded reports are always re-labeled. This is intentional: an ETL re-upload is the pipeline's signal that the report should be re-evaluated. The per-group skip is therefore active only on the backfill path, where no Report writes occur between scope evaluation and processing.

### Skip conditions (per report, inside `label_report`)

- `report.body` empty/whitespace → return immediately, no LLM calls.
- No active questions → return immediately, no LLM calls.
- Per-group idempotency (above) → an individual group's LLM call is skipped when every answer in the group is current. On the ingest path this is effectively never; on the backfill path it's the common case for groups not containing the changed question.

### Failure handling

Procrastinate retries the batch task with backoff on uncaught exceptions. Within a batch, per-report exceptions are caught inside `label_reports_in_parallel` — one report's LLM failure does not abort the rest of the batch, and the failed report is picked up by the next backfill (the scope query treats "no answer" and "stale answer" identically). Failures are logged to the `radis.labels` logger. No `AnalysisJob`/`AnalysisTask` rows are produced for the ingest path; observability for individual ingest tasks comes through Procrastinate's own job tables and the application log.

### Scale behavior

For your 1.5M-report initial bulk load arriving in batches of 100:

- Procrastinate queue grows by ~15,000 rows total, not 1.5M.
- Each `label_report_batch` task processes 100 reports concurrently (6-way thread pool) and finishes in roughly `(100 / 6) × 3 LLM-calls × ~3 s ≈ 150 s`.
- Total LLM work for the initial load is identical to a per-report design: ~17 days at default concurrency. The batch shape doesn't change throughput — it only changes queue size, operational visibility, and retry granularity. Scaling throughput requires more `llm_worker` replicas or a faster LLM, which is operational rather than a design knob in this spec.
- During the bulk-load window the `llm` queue is dominated by ingest tasks; the `llm` queue may backlog momentarily under sustained heavy ingest; this is documented as expected behavior, not a v1 blocker. Admin-triggered backfill runs at lower priority and effectively pauses until the queue catches up.

After the initial load, the per-group idempotency check (above) keeps subsequent backfills cheap. A backfill triggered by editing one question in one group touches every report once (to evaluate the skip condition and write that group's answer) but only makes ~`1/N_groups` of the LLM calls of a naive design. For 3 typical groups that's a ~3× reduction in LLM cost; for 10 groups it's ~10×.

## Execution Path 2: Backfill

Mirrors the `extractions` app's Job/Task pattern.

### Models

```python
class LabelingJob(AnalysisJob):
    default_priority = settings.LABELING_BACKFILL_PRIORITY
    urgent_priority  = settings.LABELING_BACKFILL_PRIORITY  # backfill is never urgent

    # Singleton constraint: at most one LabelingJob may be in an active status at any time.
    # Implemented as a partial unique index in the migration; the exact ORM/migration shape
    # (UniqueConstraint with expressions, RunSQL, or a database trigger) is chosen during
    # implementation planning since Django's UniqueConstraint requires either fields or
    # expressions and the right shape depends on the target Postgres version.
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

The partial-unique index (over the active-status set, declared in the initial migration) guarantees at most one active backfill. A second attempt raises `IntegrityError`, which the admin Run view translates to a flash message.

### Phase 1 — PREPARING (default queue)

A single background task builds the task scope without loading the full corpus into memory:

```python
@app.task()
def process_labeling_job(job_id: int) -> None:
    job = LabelingJob.objects.get(id=job_id)
    job.status = AnalysisJob.Status.PREPARING
    job.started_at = timezone.now()
    job.save()

    create_labeling_tasks_streaming(job)
    job.status = AnalysisJob.Status.PENDING
    job.save()

    enqueue_all_pending_tasks(job)
```

`create_labeling_tasks_streaming` iterates `Report.objects.order_by("pk").iterator(chunk_size=1000)`. For each chunk it calls `find_reports_needing_work(chunk_ids)` to identify reports with missing or stale answers, then bulk-creates `LabelingTask` rows of `LABELING_TASK_BATCH_SIZE` (default 100) reports each, committing per chunk.

`find_reports_needing_work(scope_ids: Iterable[int]) -> Iterator[int]` is the named function implementing the "needs work" predicate: the count of *non-stale, active-question* answers for the report is strictly less than the active question count. ORM sketch:

```python
def find_reports_needing_work(scope_ids):
    active_question_count = Question.objects.filter(active=True).count()
    if active_question_count == 0:
        return iter(())
    return (
        Report.objects.filter(id__in=scope_ids)
        .annotate(
            non_stale_count=Count(
                "answers",
                filter=Q(
                    answers__question__active=True,
                    answers__generated_at__gte=F("answers__question__updated_at"),
                ),
            )
        )
        .filter(non_stale_count__lt=active_question_count)
        .values_list("id", flat=True)
        .iterator()
    )
```

Equality means "fully labeled and current"; anything less means at least one missing or stale answer. This is the function referenced earlier as the "first layer of efficiency" in § "Per-group idempotency".

### Phase 2 — IN_PROGRESS (`llm` queue)

```python
@app.task(queue="llm")
def process_labeling_task(task_id: int) -> None:
    LabelingTaskProcessor(LabelingTask.objects.get(id=task_id)).start()
```

`LabelingTaskProcessor` subclasses `AnalysisTaskProcessor` and overrides `process_task`. It calls `label_reports_in_parallel(report_ids)` (the same helper the ingest batch task uses) and converts the returned `(success_count, failure_count)` into a task status:

- `failure == 0` → `task.status = SUCCESS` (and `task.message = ""`)
- `failure > 0 and success > 0` → `task.status = WARNING` (with a message naming the failure count)
- `failure > 0 and success == 0` → `task.status = FAILURE`

The base class then calls `job.update_job_state()` which promotes the overall job status accordingly. The ingest path discards the return value (no `LabelingTask` to update); per-report failures there are observable only via the log and via the next backfill picking the reports up again.

### Cancellation and resumability

- **Cancel:** admin clicks Cancel → status flips to `CANCELING`. The base processor sees this on its per-task check and marks tasks `CANCELED`.
- **Resume:** the next backfill recomputes its scope from current state. Pairs that already have a non-stale answer are skipped. Backfill is therefore idempotent and safe to start, cancel, and restart.

### Scale estimate

Throughput is bounded by LLM concurrency. With one `llm_worker` running one task at a time, the task's internal `ThreadPoolExecutor` provides `LABELING_LLM_CONCURRENCY_LIMIT` parallel slots. Each slot processes one report at a time, and each report incurs one LLM call per question group.

```text
reports_per_sec = concurrency / (groups_per_report × seconds_per_call)
```

With defaults (`concurrency=6`, `groups_per_report≈3`, `seconds_per_call≈3 s`): ≈ 0.67 reports/sec, ≈ ~17 days for 1M reports. Tunable by scaling `llm_worker` replicas (Procrastinate fan-out), raising `LABELING_LLM_CONCURRENCY_LIMIT`, or reducing question groups. A full backfill is a multi-day operation by design.

## Admin UX

### `QuestionAdmin`

Primary authoring surface.

```python
class QuestionAdmin(admin.ModelAdmin):
    list_display    = ("label", "group", "active", "text_preview", "updated_at", "answer_summary")
    list_filter     = ("active", "group")
    search_fields   = ("label", "group", "text")
    ordering        = ("group", "label")
    readonly_fields = ("created_at", "updated_at", "answer_summary")
    fieldsets = (
        (None,    {"fields": ("label", "group", "text", "active")}),
        ("Stats", {"fields": ("answer_summary", "created_at", "updated_at")}),
    )
```

- `text_preview` truncates to ~80 chars in the list view.
- `answer_summary` shows live counts: `1,234 Yes · 567 Maybe · 8,910 No · 42 stale` per question.
- `group` rendered as a `TextInput` with a `datalist` populated from existing distinct group strings — type-ahead from prior groups, prevents typos that fragment a group ("Lung" vs "Lungs").
- Unique-label enforced at the model level; the admin surfaces the friendly error.

### `LabelingJobAdmin`

The backfill cockpit. Mostly read-only — the action is the Run/Cancel button on the changelist banner.

```python
class LabelingJobAdmin(admin.ModelAdmin):
    list_display    = ("id", "status", "owner", "progress_detail", "created_at", "started_at", "ended_at")
    list_filter     = ("status",)
    readonly_fields = ("status", "owner", "progress_detail", "message", "created_at", "started_at", "ended_at")
    change_list_template = "labels/admin/labelingjob_changelist.html"

    def has_add_permission(self, request):
        return False
```

The changelist template overlays a status banner:

- **No active job present:** a `[Run backfill]` button POSTs to a custom admin view that creates the `LabelingJob` and calls `.delay()`. On `IntegrityError` (a job was just created concurrently), the view redirects with a "another backfill just started" message.
- **Active job present:** a progress panel ("PREPARING — building task list, 423,000 reports scanned" / "IN_PROGRESS — 38 / 10,247 tasks complete") with a `[Cancel]` button that POSTs to a view setting status to `CANCELING`.

`progress_detail` on the change form pulls live counts (tasks-by-status, throughput-derived ETA). No caching needed for the detail view.

### `AnswerAdmin`

Read-only, for ops/debugging.

```python
class AnswerAdmin(admin.ModelAdmin):
    list_display    = ("report", "question_label", "value", "is_stale", "generated_at")
    list_filter     = ("value", "question__group", "question")
    search_fields   = ("report__document_id", "question__label")
    raw_id_fields   = ("report", "question")
    readonly_fields = tuple(f.name for f in Answer._meta.fields)

    def has_add_permission(self, request):    return False
    def has_change_permission(self, *a):       return False
```

`is_stale` is annotated on the queryset via `F("question__updated_at") > F("generated_at")` and exposed through a `SimpleListFilter`.

### `ReportAdmin` inline

A small read-only inline showing labels per report on the Report change form:

```python
class AnswerInline(admin.TabularInline):
    model = Answer
    fields = ("question", "value", "generated_at", "is_stale")
    readonly_fields = fields
    extra = 0
    can_delete = False
    show_change_link = False
```

Added via a one-line edit to the existing `ReportAdmin.inlines` — the only cross-app admin touch.

### Intentionally omitted (v1)

- "Preview LLM call" button on `QuestionAdmin` — would create a sync LLM call from a request thread; defer to a management command or async preview later.
- Bulk "mark stale" action — staleness is already derived from `updated_at`.
- Per-question backfill — keeps the singleton constraint simple. Stale-only scope already handles the common "just added one question" case efficiently.

## End-User Surfacing

### Report detail page

A Labels region is added to the existing report detail template (the template that renders `Report.body`), wrapped as a Cotton component:

```html
<c-report-labels :report="report" />
```

Implementation lives in `radis/labels/templates/cotton/`. Reusable on any future report-card surface (collections, search result cards).

Rendering rules:

- `value=YES` → solid badge with the label color.
- `value=MAYBE` → outlined badge with a `?` suffix or warning outline, visually distinct from YES.
- `value=NO` → not rendered.
- Stale answer (`generated_at < question.updated_at`) → muted style + tooltip "label may be outdated; will be refreshed by next backfill". Rendered anyway, because the previous answer is still the best available signal.
- No answers for this report yet → small muted line "Labels pending".

Badges are grouped by `question.group` with a small group heading; empty groups (all NO) collapse silently.

Data is fetched in one round-trip via `Report.objects.prefetch_related(Prefetch("answers", queryset=Answer.objects.filter(value__in=[YES, MAYBE]).select_related("question")))` on the detail view.

### Search filter (multi-select)

Labels are exposed through the existing `SearchFilters`/`SearchForm` infrastructure — the same mechanism that already powers `modalities`, `language`, `patient_sex`, etc. No `QueryParser` extension. (Free-text `label:` syntax in the query box is listed as a possible follow-up in Non-Goals.)

**Form/filter shape.** Add a `labels: list[str]` field to `SearchFilters` (the dataclass in the search app whose existing fields are mapped to Q-objects by `_build_filter_query` in `radis/pgsearch/providers.py`). On the form, render it as a checkbox group inside the existing Filters card, populated dynamically from active `Question.label` values:

```python
# radis/search/forms.py (sketch — actual field type matches existing form style)
labels = forms.MultipleChoiceField(
    required=False,
    widget=forms.CheckboxSelectMultiple,
    choices=[],
)

def __init__(self, *args, **kwargs):
    super().__init__(*args, **kwargs)
    self.fields["labels"].choices = [
        (label, label)
        for label in Question.objects
        .filter(active=True)
        .order_by("label")
        .values_list("label", flat=True)
        .distinct()
    ]
```

**Provider integration.** Extend `_build_filter_query` in `radis/pgsearch/providers.py` with a `labels` block. Multiple selections AND together (matching the user mental model of "narrow"). Each clause is an `id__in=Subquery(...)` so the negation case (no matching answer exists) is correct should we later expose a "NOT this label" UI:

```python
if filters.labels:
    from radis.labels.models import Answer
    for label in filters.labels:
        fq &= Q(
            id__in=Answer.objects.filter(
                question__label=label,
                value__in=["YES", "MAYBE"],
            ).values("report_id")
        )
```

(Path through `report__` vs `id__` depends on whether `_build_filter_query` targets `Report` directly or `ReportSearchVector`; verify during implementation and match the existing filter blocks.) `MAYBE` is included by default — consistent with "Maybe attaches the label."

Backed by the `(question, value)` index on `Answer`. Boolean composition is handled by chaining `Q` objects with `&` as the existing filter blocks already do.

**Facet counts.** In the same filters card, each label-checkbox option is annotated with its count among the current result set:

```python
def facet_label_counts(reports_qs, top_n: int = 20) -> list[tuple[str, int]]:
    return list(
        Answer.objects
        .filter(report__in=reports_qs, value__in=["YES", "MAYBE"])
        .values("question__label")
        .annotate(c=Count("report", distinct=True))
        .order_by("-c", "question__label")[:top_n]
        .values_list("question__label", "c")
    )
```

One query, executed alongside the main search in the `SearchView.get_context_data`. The top-N cap keeps the panel bounded when many labels exist. Counts displayed next to each checkbox label (e.g., `pneumonia (1,243)`).

**Empty states.** No active questions → the labels block is hidden. Questions exist but no answers yet → counts are zero; the block shows "Labels are still being generated." in muted text.

**Non-goals here:** combining label filters with semantic similarity scoring (label filter is a binary constraint, ranking is unchanged); surfacing NO anywhere; a "stale only" filter modifier.

## Settings

Added to `radis/settings/base.py`:

```python
# Priorities (Procrastinate uses higher = sooner).
# Existing baseline:
#   EXTRACTION_DEFAULT_PRIORITY = 2 · EXTRACTION_URGENT_PRIORITY = 3
#   SUBSCRIPTION_DEFAULT_PRIORITY = 3 · SUBSCRIPTION_URGENT_PRIORITY = 4
LABELING_INGEST_PRIORITY   = env.int("LABELING_INGEST_PRIORITY",   default=1)
LABELING_BACKFILL_PRIORITY = env.int("LABELING_BACKFILL_PRIORITY", default=0)

LABELING_TASK_BATCH_SIZE       = env.int("LABELING_TASK_BATCH_SIZE",       default=100)
LABELING_LLM_CONCURRENCY_LIMIT = env.int("LABELING_LLM_CONCURRENCY_LIMIT", default=6)

LABELING_SYSTEM_PROMPT = env.str("LABELING_SYSTEM_PROMPT", default=DEFAULT_LABELING_SYSTEM_PROMPT)
```

Default prompt:

```text
You are an AI medical assistant analyzing a radiology report. Answer each of the questions
below independently, based only on what is stated or strongly implied in the report.

For each question, respond with exactly one of:
  - "YES"   — the answer is yes / the finding is present
  - "NO"    — the answer is no / the finding is absent
  - "MAYBE" — the report is genuinely ambiguous; do not use this when the answer is clear

Return the answers in JSON format matching the provided schema.

Radiology Report:
$report

Questions:
$questions
```

The Pydantic schema is built at call time via `pydantic.create_model`, with one `Literal["YES", "NO", "MAYBE"]` field per question keyed by a sanitized label. OpenAI structured-output mode enforces the answer space at the protocol level.

## Operational Considerations

### Worker / queue topology

No new Procrastinate queue, no new container. The existing `llm_worker` drains the `llm` queue; priority alone separates work:

| Source                                  | Priority |
| --------------------------------------- | -------- |
| Urgent subscriptions                    | 4        |
| Urgent extractions / default subs       | 3        |
| Default extractions                     | 2        |
| Ingest labeling (`label_report_batch`)  | 1        |
| Backfill labeling                       | 0        |

**Documented operational fact:** during sustained ingest bursts (including the initial 1.5M-report bulk load), backfill makes near-zero progress. This is the intended trade-off — newly-ingested reports get labeled first; backfill catches anything that slipped through afterward.

### Database growth

`Answer` table size ≈ reports × active questions (1.5M × 20 ≈ 30M rows for the initial corpus). Indexes are declared on the initial migration so the indexes are built while the table is empty (fast). No future ALTER on `Answer` planned.

Cascade deletes on Report and Question are CASCADE. Deleting a question with ~1.5M answers is heavy but is gated behind Django's standard delete confirmation.

### Logging

A dedicated logger `radis.labels`:

- `INFO` on `label_report_batch` task start/finish (chunk size, success/failure counts, duration).
- `WARNING` on skip (empty body, no active questions).
- `ERROR` with full traceback on per-report LLM failure inside a batch.

Ops one-liner: `docker compose logs llm_worker | grep "radis.labels"`.

### Observability

- Live progress in `LabelingJobAdmin` change form.
- A management command `uv run cli labels-status` prints corpus coverage: total reports, fully-current count, any-missing-or-stale count, per-question Yes/Maybe/No/stale counts. Uses the same scope query as backfill.
- No Prometheus metrics in v1.

### Documentation updates

- `CLAUDE.md`: add `radis.labels` to the Django Apps list; add the four new env vars; add a Troubleshooting subsection ("Labels not appearing", "Backfill stuck in PREPARING").
- `KNOWLEDGE.md`: document prompt design choices (YES/NO/MAYBE, group batching), and the rule of thumb that questions should be answerable from the report body alone.
- `example.env`: the four new env vars with comments and defaults.

### Existing-system touchpoints

Few and small, by design:

1. `radis/reports/site.py` — register create/update handlers (existing extension point). The reports app already fires these handlers wrapped in `transaction.on_commit` from every write path (admin, single-API, bulk-upsert), so our handler runs after the row is visible.
2. `radis/reports/admin.py` — add `AnswerInline` to `ReportAdmin.inlines` (one-line patch).
3. `radis/search/` (`forms.py` / `models.py`) — add `labels: list[str]` to `SearchFilters` and a `MultipleChoiceField` to the form, populated from active `Question.label` values.
4. `radis/pgsearch/providers.py` — extend `_build_filter_query` with the labels block (`id__in=Subquery(...)` per selected label, ANDed). Add `facet_label_counts` helper.
5. `radis/search/templates/search/search.html` (or the existing filters card include) — render the labels checkbox group with facet counts inside the Filters card.
6. `radis/settings/base.py` — new settings block (additive).
7. `radis/reports/templates/reports/report_detail.html` — include `<c-report-labels />`.

## Testing Strategy

### Unit tests (no DB)

- **Prompt rendering** (`test_prompts.py`) — snapshot tests of `render_questions_prompt` for representative question sets and unicode content.
- **Schema building** (`test_schemas.py`) — `build_yes_no_maybe_schema` produces a Pydantic model that validates correct payloads and rejects unknown values, extra fields, and missing fields. Two questions colliding after label sanitization raise.

### Model / DB tests

- **Stale detection** (`test_stale_detection.py`) — directly assert the predicate behavior over hand-built rows.
- **Backfill scope query** (`test_scope.py`) — four reports with the four canonical answer states (none / partial / all-but-one-stale / all-current); the scope query returns exactly the three needing work. This is the most important integration test.
- **Singleton backfill** (`test_singleton.py`) — second concurrent active `LabelingJob` raises `IntegrityError`; allowed once the first becomes `CANCELED`/`SUCCESS`/`FAILURE`.
- **Cascade and uniqueness** (`test_models.py`) — question delete cascades to answers; `(report, question)` unique enforced; `update_or_create` upserts.

### Integration tests (LLM mocked)

- **Ingest handler chunks correctly** (`test_signals.py`) — handler called with 250 reports defers exactly 3 `label_report_batch` tasks (100 + 100 + 50) at the ingest priority; handler called with 10 reports defers exactly 1 task; report IDs are preserved across chunks.
- **`label_report` end-to-end** (`test_label_report.py`) — monkeypatched `ChatClient.extract_data`. Two question groups → two LLM calls with the right questions; answers upserted; `generated_at` bumped on re-run; MAYBE preserved.
- **Per-group idempotency** (`test_idempotency.py`) — four scenarios:
  1. All answers current → no LLM calls.
  2. One question in group A edited → only group A's LLM call fires.
  3. Report body updated (`report.updated_at` advanced) → all groups' LLM calls fire.
  4. Brand-new report with no answers → all groups' LLM calls fire.
  Each scenario asserts the exact set of LLM calls made via a mock counter.
- **Skip conditions** (`test_skips.py`) — empty body / no active questions / inactive question → no LLM call.
- **`label_reports_in_parallel` partial failure** (`test_parallel.py`) — three report IDs, one raises in the LLM mock; the helper returns `(2, 1)`, the two successful reports have `Answer` rows, the failed one does not, and the exception is logged but not re-raised.
- **Backfill task processor** (`test_processor.py`) — three reports in one task; partial failure on one yields task `WARNING`; successful reports still have answers written; `update_job_state` promotes the job correctly.

### Admin tests

- **`QuestionAdmin`** — duplicate label surfaces as a form error.
- **`LabelingJobAdmin`** — Run with no active job creates and delays a job; Run while one is active flashes the conflict message; Cancel flips status to `CANCELING`.

### Search integration tests

- **SearchFilters carries labels** (`test_search_filters.py`) — `SearchFilters(labels=["pneumonia"])` round-trips; default is empty list.
- **pgsearch translator** (`test_label_filter.py`) — 3 reports × 2 questions in a real DB. `filters.labels=["pneumonia"]` includes reports with YES/MAYBE for pneumonia and excludes others. `filters.labels=["pneumonia", "effusion"]` returns only reports that have both labels applied (AND semantics).
- **Facet counts** (`test_facet_counts.py`) — 10 reports with a known answer matrix; `facet_label_counts(result_qs, top_n=N)` returns the right `(label, count)` pairs in descending-count order, capped at N.

### UI tests (Playwright, `@pytest.mark.acceptance`)

- **Report detail** — YES badge visible, MAYBE distinctly styled, NO not rendered; stale tooltip present.
- **Search filters card** — labels block lists labels with counts; checking a label adds it to the URL `labels=` query param; result list narrows.

### One real-LLM acceptance test (`@pytest.mark.acceptance`, `cpu` profile)

- Create one question ("Is the chest clear?"), ingest a synthetic report stating "No abnormalities, lungs clear.", wait for the ingest `label_report_batch` task to drain, assert an `Answer` row with `value=YES` appears. Sanity that the wiring works against the real LLM stack. Not a regression suite.

### Out of scope (test-wise)

- Performance / load tests on 1M-row backfill. Recommended to benchmark on staging before turning on in production, but not part of the suite.
- Procrastinate's retry semantics.
- Migrations on existing data — both tables start empty.

## Risks and Open Questions

- **LLM cost on the 1M-report backfill.** Multi-day run at the documented throughput; cost should be modeled before turning on for production. Mitigation: backfill is cancelable at any time.
- **Question wording quality.** The system is only as good as the questions. No safeguards against vague or leading questions in v1; admins are expected to author carefully.
- **Report deletion blast radius.** A report deletion cascades to its `Answer` rows. Standard Django behavior; not unique to this feature.
- **Label drift through question rewordings.** Two questions intentionally targeting the same clinical concept can produce different LLM answers if their wording differs. The unique-`label` constraint prevents this when both questions share a label, but admins are otherwise free to author overlapping questions. v1 does no automated detection.

## Out of Scope (Future Work)

- Manual override / user-correction of labels.
- Versioned answer history.
- Per-question backfill triggered from the question admin.
- A "stale" search filter modifier.
- LLM call preview in the question admin.
- Prometheus metrics on labeling throughput.
- Localized prompts.
