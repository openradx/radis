# Auto-Labeling Feature — Design

**Status:** Approved design, ready for implementation planning
**Date:** 2026-05-21
**Owner:** Kai Schlamp

## Overview

Add an auto-labeling feature to RADIS that classifies radiology reports against admin-defined questions using an LLM. Each question carries the label it produces and a group string used to batch related questions into a single LLM call. The LLM answers each question with `YES`, `NO`, or `MAYBE`; `YES` and `MAYBE` both attach the label to the report (with `MAYBE` flagged as uncertain). Answers are stored per `(report, question)` pair.

Two execution paths share the same labeling logic:

1. **Per-report path** — A `post_save` signal on `Report` (creation and update) enqueues a background task that labels that report against all active questions.
2. **Backfill path** — An admin-triggered `LabelingJob` walks the existing report corpus and produces missing or stale answers. Only one backfill may be active at a time.

Labels surface on the report detail page (badges) and in search (`label:` query filter + facet panel).

## Goals

- Admins can author labeling questions in the Django admin and see them applied to reports automatically.
- New and updated reports are labeled in the background without blocking ingest.
- Existing reports (up to ~1M scale) can be backfilled.
- Edits to a question naturally produce stale answers, which the next backfill refreshes.
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

## Execution Path 1: Per-Report Labeling

Driven by `post_save`-style hooks on `Report`. Used for both new reports and report updates.

### Trigger

Register handlers using the existing extension points in `radis.reports.site`:

```python
register_reports_created_handler(_label_report_handler)
register_reports_updated_handler(_label_report_handler)
```

The handler schedules an enqueue via `transaction.on_commit`, so we never enqueue against a not-yet-visible row:

```python
def _label_report_handler(report: Report) -> None:
    transaction.on_commit(lambda: label_single_report.defer(report_id=report.id))
```

### Task

```python
@app.task(queue="llm")
def label_single_report(report_id: int) -> None:
    label_report(report_id)
```

Configured with priority `LABELING_PER_REPORT_PRIORITY` (default `1`) — higher than backfill (`0`) so ingest is always reflected promptly, but lower than urgent extractions/subscriptions so it never delays user-initiated work.

### Core function

The same function is used by the backfill processor.

```python
def label_report(report_id: int) -> None:
    report = Report.objects.get(id=report_id)
    if not report.body or not report.body.strip():
        return
    questions_by_group = group_active_questions_by_group()
    if not questions_by_group:
        return
    client = ChatClient()
    for group_str, questions in questions_by_group.items():
        Schema = build_yes_no_maybe_schema(questions)
        prompt = render_questions_prompt(report.body, questions)
        parsed = client.extract_data(prompt, Schema)
        upsert_answers(report, questions, parsed)
```

One LLM call per question group. `upsert_answers` uses `update_or_create` per `(report, question)`. `generated_at` is bumped on every write via `auto_now=True`.

### Skip conditions

- `report.body` empty/whitespace.
- No active questions.

### Failure handling

Procrastinate retries with backoff on transient failures (LLM timeout, network). After exhausted retries, the failure is logged to the `radis.labels` logger; no `AnalysisJob`/`AnalysisTask` rows are produced for the per-report path. Reports that fail to label this way are eventually re-attempted by the next backfill (the same scope query treats "no answer" and "stale answer" identically).

### Burst behavior

Bursts of ~300 reports produce 300 queue rows. The per-report task processes one report sequentially through its question groups (~3 LLM calls × ~3 s ≈ ~9 s per report); the queue drains as Procrastinate workers pick up tasks. The exact wall-clock duration depends on the deployed worker count and per-worker Procrastinate concurrency. The `llm` queue may backlog momentarily under sustained heavy ingest; that is documented as expected behavior, not a v1 blocker.

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

`create_labeling_tasks_streaming` iterates `Report.objects.order_by("pk").iterator(chunk_size=1000)`. For each chunk it identifies reports needing work and bulk-creates `LabelingTask` rows of `LABELING_TASK_BATCH_SIZE` (default 100) reports each, committing per chunk.

The "needs work" predicate is: the count of *non-stale, active-question* answers for the report is strictly less than the active question count. ORM sketch:

```python
needs_work = (
    Report.objects.filter(id__in=chunk_ids)
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
)
```

Equality means "fully labeled and current"; anything less means at least one missing or stale answer.

### Phase 2 — IN_PROGRESS (`llm` queue)

```python
@app.task(queue="llm")
def process_labeling_task(task_id: int) -> None:
    LabelingTaskProcessor(LabelingTask.objects.get(id=task_id)).start()
```

`LabelingTaskProcessor` subclasses `AnalysisTaskProcessor` and overrides `process_task`. For each report in the task, it calls `label_report(report_id)` — the same core function used by the per-report path. Within the task it uses a `ThreadPoolExecutor(max_workers=LABELING_LLM_CONCURRENCY_LIMIT)`, mirroring extractions. Status updates and `update_job_state` come from the base class.

### Cancellation and resumability

- **Cancel:** admin clicks Cancel → status flips to `CANCELING`. The base processor sees this on its per-task check and marks tasks `CANCELED`.
- **Resume:** the next backfill recomputes its scope from current state. Pairs that already have a non-stale answer are skipped. Backfill is therefore idempotent and safe to start, cancel, and restart.

### Scale estimate

Throughput is bounded by LLM concurrency. With one `llm_worker` running one task at a time, the task's internal `ThreadPoolExecutor` provides `LABELING_LLM_CONCURRENCY_LIMIT` parallel slots. Each slot processes one report at a time, and each report incurs one LLM call per question group.

```
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
    list_display    = ("id", "status", "owner", "progress", "created_at", "started_at", "ended_at")
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

```
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

### Search filter (`label:`)

Two pieces: a query syntax for power users and a UI control for everyone.

**Query syntax.** Extend `QueryParser` with `label:` as a known field filter:

| Query                                | Meaning                                                                                   |
| ------------------------------------ | ----------------------------------------------------------------------------------------- |
| `label:pneumonia`                    | reports where some answer has `question.label="pneumonia"` AND `value IN (YES, MAYBE)`    |
| `label:pneumonia AND label:effusion` | intersection                                                                              |
| `label:pneumonia OR label:effusion`  | union                                                                                     |
| `NOT label:pneumonia`                | reports without that label applied (no YES/MAYBE)                                         |

`MAYBE` is included by default — consistent with "Maybe attaches the label."

**Provider integration.** Each search provider translates the parsed `label:` AST node to its query language. In v1 only `radis.pgsearch` needs a translation. The positive case uses a JOIN through `answers`:

```python
def translate_label_filter(label_value: str) -> Q:
    return Q(
        id__in=Answer.objects.filter(
            question__label=label_value, value__in=["YES", "MAYBE"]
        ).values("report_id")
    )
```

Using `id__in=Subquery(...)` instead of a direct `answers__…` JOIN keeps the negation correct: `~Q(id__in=...)` cleanly means "no matching Answer row exists" rather than the wrong "some Answer row doesn't match". Backed by the `(question, value)` index on `Answer`. Boolean composition (AND/OR/NOT) is then handled by the existing parser-to-Q machinery without further special cases.

**UI control.** A facet panel on the search results page:

- Lists the top N most-applied labels among the current result set, with counts (`pneumonia 1,243 · effusion 892 · ...`).
- Each label is a checkbox. Checking adds `label:X` to the search query (multiple → AND). Unchecking removes it.
- Counts: `Answer.objects.filter(report__in=result_qs, value__in=["YES", "MAYBE"]).values("question__label").annotate(c=Count("report", distinct=True)).order_by("-c")[:N]`. One query, executed alongside the main search.
- Rendered server-side; updates via HTMX on checkbox change.

**Empty states.** No questions exist → facet panel hidden. Questions exist but no answers yet → "Labels are still being generated."

**Non-goals here:** stale filter (`label:foo:stale`); combining label filters with semantic similarity scoring (label filter is a binary constraint, ranking is unchanged); surfacing NO anywhere.

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

| Source                              | Priority |
| ----------------------------------- | -------- |
| Urgent subscriptions                | 4        |
| Urgent extractions / default subs   | 3        |
| Default extractions                 | 2        |
| Per-report labeling                 | 1        |
| Backfill labeling                   | 0        |

**Documented operational fact:** during sustained ingest bursts, backfill makes near-zero progress. This is the intended trade-off.

### Database growth

`Answer` table size ≈ reports × active questions (1M × 20 ≈ 20M rows). Indexes are declared on the initial migration so the indexes are built while the table is empty (fast). No future ALTER on `Answer` planned.

Cascade deletes on Report and Question are CASCADE. Deleting a question with ~1M answers is heavy but is gated behind Django's standard delete confirmation.

### Logging

A dedicated logger `radis.labels`:

- `INFO` on per-report task start/finish (report ID + duration).
- `WARNING` on skip (empty body, no active questions).
- `ERROR` with full traceback on LLM failure after retries.

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

1. `radis/reports/site.py` — register create/update handlers (existing extension point).
2. `radis/reports/admin.py` — add `AnswerInline` to `ReportAdmin.inlines` (one-line patch).
3. `radis/search/parser.py` — register `label:` as a known field filter (existing extension point).
4. `radis/pgsearch/` — translator for the `label:` AST node into a `Q` object.
5. `radis/settings/base.py` — new settings block (additive).
6. Templates — new Cotton component, included on the report detail template and the search facet panel.

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

- **Per-report signal fires post-commit** (`test_signals.py`) — using `django_capture_on_commit_callbacks`, the task is enqueued on commit, not before; idempotent on update.
- **`label_report` end-to-end** (`test_label_report.py`) — monkeypatched `ChatClient.extract_data`. Two question groups → two LLM calls with the right questions; answers upserted; `generated_at` bumped on re-run; MAYBE preserved.
- **Skip conditions** (`test_skips.py`) — empty body / no active questions / inactive question → no LLM call.
- **Backfill task processor** (`test_processor.py`) — three reports in one task; partial failure on one yields task `WARNING`; successful reports still have answers written; `update_job_state` promotes the job correctly.

### Admin tests

- **`QuestionAdmin`** — duplicate label surfaces as a form error.
- **`LabelingJobAdmin`** — Run with no active job creates and delays a job; Run while one is active flashes the conflict message; Cancel flips status to `CANCELING`.

### Search integration tests

- **Parser** (`test_parser_labels.py`) — `label:pneumonia`, `label:foo AND label:bar`, `NOT label:foo` produce the expected AST.
- **pgsearch translator** (`test_label_filter.py`) — 3 reports × 2 questions in a real DB; the filter includes/excludes correctly.
- **Facet counts** — 10 reports with a known matrix; aggregation returns the right `(label, count)` pairs in the right order.

### UI tests (Playwright, `@pytest.mark.acceptance`)

- **Report detail** — YES badge visible, MAYBE distinctly styled, NO not rendered; stale tooltip present.
- **Search facet** — facet panel lists labels with counts; checking a label appends `label:X` to the query and updates the result list via HTMX.

### One real-LLM acceptance test (`@pytest.mark.acceptance`, `cpu` profile)

- Create one question ("Is the chest clear?"), ingest a synthetic report stating "No abnormalities, lungs clear.", wait for the per-report task, assert an `Answer` row with `value=YES` appears. Sanity that the wiring works against the real LLM stack. Not a regression suite.

### Out of scope (test-wise)

- Performance / load tests on 1M-row backfill. Recommended to benchmark on staging before turning on in production, but not part of the suite.
- Procrastinate's retry semantics.
- Migrations on existing data — both tables start empty.

## Risks and Open Questions

- **LLM cost on the 1M-report backfill.** Multi-day run at the documented throughput; cost should be modeled before turning on for production. Mitigation: backfill is cancelable at any time.
- **Question wording quality.** The system is only as good as the questions. No safeguards against vague or leading questions in v1; admins are expected to author carefully.
- **`label:` token in user queries.** If a user's free-text search happens to contain `label:` (unlikely but possible), the parser will treat it as a field filter. Matches behavior of other field filters in the parser; acceptable.
- **Report deletion blast radius.** A report deletion cascades to its `Answer` rows. Standard Django behavior; not unique to this feature.

## Out of Scope (Future Work)

- Manual override / user-correction of labels.
- Versioned answer history.
- Per-question backfill triggered from the question admin.
- A "stale" search filter modifier.
- LLM call preview in the question admin.
- Prometheus metrics on labeling throughput.
- Localized prompts.
