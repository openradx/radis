# Auto-Labeling Feature — Design

**Status:** Implemented (consolidated final-state spec)
**Date:** 2026-05-21 (consolidated 2026-06-10)
**Owner:** Kai Schlamp

> This is the single consolidated design spec for the auto-labeling feature. It folds in the
> later refinements that shipped (name-keyed LLM schema fields, a YES/NO-only group gate, and
> the search-page **Filters** widget that replaced the typed `label:` query syntax) and
> describes the system as actually built.

## Overview

Auto-labeling classifies radiology reports against admin-defined labels using an LLM. Labels are
organised into `LabelGroup`s. Each group carries a **gate question** — a single upfront Yes/No
applicability screen asked before the group's labels. For each label the LLM assigns exactly one
of five buckets: `PRESENT`, `LIKELY`, `POSSIBLE`, `ABSENT`, `UNMENTIONED`. The three "some
evidence" buckets (`PRESENT`, `LIKELY`, `POSSIBLE`) attach the label to the report; `ABSENT` and
`UNMENTIONED` are stored but never surface. Results are stored per `(report, label)`; gate answers
per `(report, label_group)`.

The gate is intentionally a different value space from the labels. A gate question ("Is this a
Head CT?") is a categorical *applicability* check — Yes/No — answering "do this group's labels
even apply to this report?". The five buckets answer a different question per label — "does this
finding appear, and how strongly?" — where `UNMENTIONED` (the report never discusses the topic) is
a genuinely distinct state a gate has no analogue for.

Two execution paths share the same labeling logic:

1. **Periodic scan path** — A Procrastinate periodic task runs on a configurable cron schedule. It
   finds reports created since the last scan tick and enqueues batched labeling tasks. If a
   backfill (or a prior scan job) is active, the scan yields for that tick without enqueuing and
   without advancing the checkpoint.
2. **Backfill path** — An admin-triggered `LabelingJob` walks the existing corpus and produces
   missing or stale results. Used for the initial bulk labeling and any future large-scale
   re-labeling (e.g. after a label edit). Only one job may be active at a time.

Labels surface on the report detail page (badges) and in search (a multi-select **Filters**-panel
widget, OR semantics across selected labels).

## Goals

- Admins author label groups (with gate questions) and labels (name + description) in the Django
  admin and see them applied automatically.
- New and updated reports are labeled in the background without blocking ingest.
- Existing reports (up to ~1M scale) can be backfilled.
- Gating reduces LLM calls by skipping irrelevant label groups entirely.
- Edits to a label or gate question naturally produce stale results, which the next backfill
  refreshes.
- End users see applied labels on each report and can filter search by label.

## Non-Goals (v1)

- Manual label editing / user correction.
- Versioned result history (only the latest result per `(report, label)` is kept).
- Per-label backfill targeting (backfill is system-wide).
- A bucket-specific search syntax (`label:foo:present`).
- Surfacing `ABSENT`/`UNMENTIONED` anywhere user-facing.
- Performance dashboards beyond the live status in the `LabelingJob` admin and `labels_status`.
- Localized prompts — labels and reports may be in any language and are passed verbatim.
- Web-based label/gate management or job-progress web pages — everything stays in Django admin.

## Data Model

A Django app `radis.labels` with the following models (see `radis/labels/models.py`).

```python
class LabelGroup(models.Model):
    name          = models.CharField(max_length=100, unique=True)
    gate_question = models.TextField()        # upfront Yes/No screening question for this group
    updated_at    = models.DateTimeField(auto_now=True)  # drives gate stale detection

class Label(models.Model):
    group       = models.ForeignKey(LabelGroup, on_delete=models.CASCADE, related_name="labels")
    name        = models.CharField(max_length=100)   # label string that surfaces (e.g. "pneumonia")
    description = models.TextField()                  # definition sent to the LLM
    active      = models.BooleanField(default=True)
    created_at  = models.DateTimeField(auto_now_add=True)
    updated_at  = models.DateTimeField(auto_now=True)  # drives result stale detection
    # Meta: UniqueConstraint(["name"]); Index(["active"])

class LabelResult(models.Model):
    class Value(models.TextChoices):
        PRESENT, LIKELY, POSSIBLE, ABSENT, UNMENTIONED
    SURFACING_VALUES = (Value.PRESENT, Value.LIKELY, Value.POSSIBLE)
    report       = FK(Report,  related_name="label_results")
    label        = FK(Label,   related_name="results")
    value        = CharField(choices=Value.choices)
    generated_at = DateTimeField(auto_now=True)
    # Meta: UniqueConstraint(["report","label"]); Index(["label","value"]); Index(["report"])

class GateAnswer(models.Model):
    class Value(models.TextChoices):
        YES, NO
    report       = FK(Report,     related_name="gate_answers")
    label_group  = FK(LabelGroup, related_name="gate_answers")
    value        = CharField(choices=Value.choices)
    generated_at = DateTimeField(auto_now=True)
    # Meta: UniqueConstraint(["report","label_group"]); Index(["label_group","value"])

class LabelingScanCheckpoint(models.Model):     # singleton, pk pinned to 1
    last_scanned_at = DateTimeField(null=True, blank=True)
    # CheckConstraint(Q(id=1)); save() forces pk=1
```

### Stale detection

- **Result stale:** `LabelResult.generated_at < F("label__updated_at")`
- **Gate answer stale:** `GateAnswer.generated_at < F("label_group__updated_at")`

No snapshot columns, no `is_stale` flags. Both comparisons are pure joins over indexed columns.

### Buckets and surfacing

All five bucket values are **stored**. `ABSENT`/`UNMENTIONED` produce real `LabelResult` rows, so
stale detection and the backfill's "needs work" predicate treat a label that came back
`ABSENT`/`UNMENTIONED` as *done*, with no re-labeling churn. Surfacing (report badges and search)
filters to `LabelResult.SURFACING_VALUES`. The control flow never branches on a label's bucket
value; the LLM returns a bucket and we store it.

### Constraints

- `LabelGroup.name` and `Label.name` are unique. Name uniqueness is also the contract for the
  name-keyed LLM schema (below).
- `(report, label)` and `(report, label_group)` are unique; re-labeling/re-evaluation replaces via
  `update_or_create`.
- Cascade deletes on all FKs (group → labels + gate answers; label → results).

### Migration

A single `labels/0001_initial` creates all tables, indexes, and constraints, and folds in the
singleton partial unique index via `RunSQL` (below). The feature was never in production, so the
intermediate migrations were squashed into this one initial migration.

## Singleton: one active LabelingJob

`LabelingJob` (subclass of `core.AnalysisJob`) and `LabelingTask` (subclass of `core.AnalysisTask`)
reuse the Job → Task pattern. A `trigger` field distinguishes `SCAN` from `MANUAL`; `scan_from` is
set for SCAN jobs (the checkpoint timestamp at creation) and `None` for MANUAL. `owner` is nullable
(scan jobs have no human owner). `LabelingTask.reports` is an M2M so one task covers a batch.
Labeling jobs never send completion mail (`_send_job_finished_mail` is a no-op; there is no mail
template and scan jobs are owner-less).

**At most one `LabelingJob` may be active at a time**, enforced at the DB level by a partial unique
index over a constant expression (Django's `UniqueConstraint` cannot express this):

```sql
CREATE UNIQUE INDEX one_active_labeling_job
ON labels_labelingjob ((true))
WHERE status IN ('UV', 'PR', 'PE', 'IP', 'CI');   -- ACTIVE_STATUSES DB codes
```

Every active row shares the key `(true)`, so a second active insert raises `IntegrityError`.
Terminal statuses (`SU`/`WA`/`FA`) are excluded, so finished jobs never block a new one.

## Execution Path 1: Periodic Incremental Scan

`incremental_label_scan` is an `@app.periodic(cron=settings.LABELING_SCAN_CRON)` task. The
`LabelingScanCheckpoint` (single row, `pk=1`) tracks `last_scanned_at` — the scan window is
`created_at >= last_scanned_at`. Logic per tick (Procrastinate passes the scheduled tick time):

1. If any `LabelingJob` is active → return immediately, **checkpoint unchanged** (yield to the
   active job; the next tick resumes from the same point and covers everything since).
2. First run (`last_scanned_at is None`) → record `now` and exit without creating a job. Existing
   reports belong to a manual backfill.
3. No active labels → return **without advancing**, so re-activation still covers the gap.
4. If reports exist with `created_at >= last_scanned_at` → create a `LabelingJob(trigger=SCAN,
   scan_from=last_scanned_at)` and `delay()` it.
5. Advance the checkpoint to `now`.

The periodic task only decides whether to create a job and advances the checkpoint; all report
iteration, task creation, and LLM work happen in the shared Job → Task machinery. At ~100
reports/day with a daily cron, each tick produces at most one job with one task — no task explosion
tied to ETL call size.

## Execution Path 2: Manual Backfill

Same Job/Task machinery; differs only by `trigger` and report scope (full corpus vs recent window).

### Phase 1 — PREPARING (default queue): `process_labeling_job`

- Accepts only `PENDING`/`PREPARING` entry states. Any other state (a spurious re-fire on a
  running/finished job) logs a warning and bails — it must not wipe in-flight tasks.
- `job.tasks.all().delete()` at the top makes prep idempotent across Procrastinate retries (only
  this job's own partial tasks are cleared). `started_at` is preserved across retries.
- Streams the scope queryset into `LABELING_TASK_BATCH_SIZE` tasks using a chunked server-side
  cursor (`.iterator(chunk_size=...)`), so the scope is frozen at cursor-open time — reports
  ingested afterward are invisible to this job (the scan checkpoint, held frozen while the job is
  active, catches them later).
- **Empty scope** (e.g. a backfill where everything is already fresh) finishes the job `SUCCESS`
  immediately instead of leaving it stuck in `PENDING` — `PENDING` is active and would block the
  singleton index forever.
- Scope:
  - **SCAN** (`scan_from` set): `Report.objects.filter(created_at__gte=scan_from).order_by("pk")`.
  - **MANUAL** (`scan_from is None`): `_needs_work_queryset(active_group_count).order_by("pk")`.

### "Needs work" predicate (`radis/labels/scope.py`)

Used only by the backfill. A report needs work if **either**:

- **Condition A** — missing or stale gate answers for any active group
  (`non_stale_gate_count < active_group_count`).
- **Condition B** — there exists a fresh `YES` gate answer whose group contains at least one active
  label without a fresh `LabelResult` for this report ("not fresh" = no row, or
  `result.generated_at < label.updated_at`). The inner `Exists` uses a double `OuterRef`.

`active_label_count` is deliberately not a parameter: NO-gated groups have no `LabelResult` rows by
design, so comparing a flat result count to the active-label count would always re-flag them.

### Phase 2 — IN_PROGRESS (`llm` queue): `process_labeling_task`

`LabelingTaskProcessor` (subclass of `AnalysisTaskProcessor`) calls `label_report(report_id)` for
each report in the task under a `ThreadPoolExecutor(max_workers=LABELING_LLM_CONCURRENCY_LIMIT)`. A
single report's exception is caught and logged with full traceback; it downgrades the task to
`WARNING` rather than failing the batch. `db.close_old_connections()` is called per worker thread
and at task end.

### Cancellation and resumability

- **Cancel** → `CANCELING`; the base processor marks tasks `CANCELED`.
- **Resume** → the next backfill recomputes scope from current state; non-stale pairs are skipped.
  Backfill is idempotent and safe to start, cancel, and restart.
- **Crash mid-prep** → Procrastinate retries; the top-of-task delete wipes partial tasks first.

## Core Labeling Function (`radis/labels/labeling.py`)

`label_report(report_id)` is the single function used by both paths. Nothing branches on a label's
bucket value — the LLM returns a bucket per label and it is stored as-is.

### Dynamic schema and generic prompt

Labels/groups are authored at runtime, so the structured-output schema is **generated on the fly**
from DB rows (`radis/labels/utils/schemas.py`):

- **The schema enforces the choice.** Each label/group becomes one required field whose type is a
  fixed enum (`BucketValue` = the five buckets; `GateValue` = YES/NO). The enum *types* are static;
  only the *set of fields* is dynamic.
- **The prompt teaches the choice.** A static, generic prompt (`utils/prompts.py`) explains what
  each value means and carries the report body. It contains **no label-specific text** — only
  `$report` is substituted.
- **Fields are keyed by name.** A label field is keyed by `lbl.name` (description =
  `lbl.description`); a gate field by `g.name` (description = `g.gate_question`). The LLM sees the
  name as the JSON property key (self-documenting). `Label.name`/`LabelGroup.name` uniqueness makes
  key collisions impossible. The caller maps names back to ids inline — no parse helpers needed.

```python
def build_label_classification_schema(labels):
    return create_model("LabelClassification",
        **{lbl.name: (BucketValue, Field(description=lbl.description)) for lbl in labels})

def build_gate_schema(groups):
    return create_model("GateScreening",
        **{g.name: (GateValue, Field(description=g.gate_question)) for g in groups})
```

The builders live in `radis/labels/utils/` and import `pydantic.create_model` and
`radis.chats.utils.chat_client.ChatClient` (the same edge extractions uses) — **nothing** from
`radis.extractions`, keeping the apps decoupled. A unit test asserts `BucketValue`/`GateValue`
equal `LabelResult.Value`/`GateAnswer.Value` (drift guard). Every field is required, so every label
gets a bucket and every group a gate answer; empty subsets never reach the builders.

### Flow

1. Skip if body empty/whitespace or no active groups.
2. Load existing gate answers; a group needs re-gating if its answer is missing or stale.
3. **Phase 1 — Gate:** batch groups-needing-gate `LABELING_GATE_BATCH_SIZE` at a time; one LLM call
   per batch returns `{g.name: "YES"|"NO"}`.
4. **Phase 2 — per group:**
   - Re-evaluated this run → `update_or_create` the gate answer; if it flips `YES→NO`, delete the
     group's results (gate save + delete wrapped in `transaction.atomic()` so an interrupted flip
     can't orphan results that condition B would never re-detect). If now `YES`, run
     stale/missing labels.
   - Fresh gate → use stored value; `YES` runs stale/missing labels, `NO` skips the group.
5. `_run_label_set` builds the dynamic schema for the stale/missing labels, one LLM call, maps the
   response back to ids inline, `update_or_create` per label.

`_get_stale_or_missing_labels` is one query that answers both "should we run?" and "what to run?":
labels whose result is missing or stale. A label that came back `ABSENT`/`UNMENTIONED` still has a
fresh row → excluded.

### Decision table

*Gate re-evaluated (stale or no prior answer):*

| Was | Now | Action |
|---|---|---|
| YES | YES | Save gate + run stale/missing labels (may be zero) |
| YES | NO  | Save gate + delete all results for group |
| NO / none | YES | Save gate + run all labels (none have results) |
| NO / none | NO  | Save gate *(no results to delete)* |

*Gate fresh (no re-evaluation):* `YES` → run stale/missing labels (skip if all fresh); `NO` → skip
group.

### Failure handling

Per-report exceptions are caught inside `LabelingTaskProcessor`; one failure doesn't abort the
batch (task → `WARNING`). A report that fails in a scan job is not retried by the next scan (the
checkpoint advanced past its `created_at`); the next manual backfill catches it via the
missing-results predicate. Procrastinate retries the task only on uncaught (infra) exceptions.

## Admin UX (`radis/labels/admin.py`)

As built — read-only monitoring plus a backfill button; no live-count widgets or progress banners.

- **`LabelGroupAdmin`** — authoring: `list_display=(name, gate_question, updated_at)`,
  `search_fields=("name",)` (required for `LabelAdmin` autocomplete), `updated_at` read-only.
- **`LabelAdmin`** — authoring: `autocomplete_fields=["group"]`,
  `list_display=(name, group, active, updated_at)`, `list_filter=(active, group)`, search on
  name/group/description, `created_at`/`updated_at` read-only.
- **`LabelResultAdmin`, `GateAnswerAdmin`, `LabelingTaskAdmin`** — fully read-only (no add/change/
  delete) via a shared `_ReadOnlyAdmin` base; list/filter/search + an `is_stale` display column on
  results and gate answers.
- **`LabelingScanCheckpointAdmin`** — read-only view of the checkpoint timestamp.
- **`LabelingJobAdmin`** — read-only monitoring with a custom changelist template adding a
  **"Run backfill now"** button (Django actions require a row selection; this does not). The view
  creates a `MANUAL` `PENDING` job inside `transaction.atomic()` + `except IntegrityError`, relying
  on the singleton index and reporting a friendly "already active" message on conflict.
  `has_add_permission` is `False`; every field is read-only. `trigger` is in `list_display` to
  distinguish scan vs manual at a glance.
- **`ReportAdmin` inline** — `LabelResultInline` (read-only) on the report change form.

## End-User Surfacing

### Report detail page

`Report.surfacing_label_results` (cached property) returns the report's `SURFACING_VALUES` results,
`select_related` on label+group, ordered by group then label name. A cotton component
`report_labels` renders them as badges grouped by `label_group.name`. `ABSENT`/`UNMENTIONED` are
never shown.

### Search filter — Filters-panel widget (replaces typed `label:` syntax)

Users filter by selecting one or more active labels from a multi-select listbox in the search
**Filters** panel, mirroring the modalities filter. The earlier typed `label:<name>` query syntax
(and its parser) was removed in favour of this widget — typing the prefix was awkward and
undiscoverable, and a literal `label:foo` typed now is ordinary search text.

| Decision | Choice | Rationale |
|---|---|---|
| Widget | Multi-select listbox (`size=6`) | Mirrors modalities; least new code. |
| Which labels | Active only (`active=True`) | Inactive labels stop getting new results. |
| Ordering | Alphabetical by `name` | Names are globally unique → flat list is unambiguous. |
| Combine | **OR** (any selected label surfaces) | Matches modalities (`__in`). |

Wiring:

- `SearchForm` (`radis/search/forms.py`) adds `labels = forms.MultipleChoiceField(required=False)`,
  populated in `__init__` from active labels (alphabetical, `size=6`); the field is included in the
  layout **only when at least one active label exists** (no empty listbox).
- `SearchFilters` (`radis/search/site.py`) gains `labels: list[str]`; `SearchView` threads
  `form.cleaned_data["labels"]` in.
- `_build_filter_query` (`radis/pgsearch/providers.py`) adds a single
  `Q(report__in=<Report.objects.filter(label_results__label__name__in=labels,
  label_results__value__in=SURFACING_VALUES).values("pk")>)` — deduplicated, OR across labels,
  surfacing buckets only regardless of staleness.

## Settings (`radis/settings/base.py`, `example.env`)

```python
LABELING_JOB_PRIORITY          = env.int("LABELING_JOB_PRIORITY",          default=1)   # scan and backfill share one priority (only one runs at a time)
LABELING_TASK_BATCH_SIZE       = env.int("LABELING_TASK_BATCH_SIZE",       default=100)
LABELING_LLM_CONCURRENCY_LIMIT = env.int("LABELING_LLM_CONCURRENCY_LIMIT", default=6)
LABELING_GATE_BATCH_SIZE       = env.int("LABELING_GATE_BATCH_SIZE",       default=10)
LABELING_SCAN_CRON             = env.str("LABELING_SCAN_CRON",             default="0 2 * * *")
LABELING_SYSTEM_PROMPT         = env.str("LABELING_SYSTEM_PROMPT",         default=_DEFAULT_LABELING_SYSTEM_PROMPT)
LABELING_GATE_SYSTEM_PROMPT    = env.str("LABELING_GATE_SYSTEM_PROMPT",    default=_DEFAULT_GATE_SYSTEM_PROMPT)
```

Both prompts are generic — they carry no label/group-specific text (that rides in each schema
field's `description=`) and substitute only `$report`. The default label prompt enumerates the five
bucket meanings; the default gate prompt enumerates YES/NO. The labeling task reuses the existing
`ChatClient`/LLM provider settings — no new LLM endpoint config.

## Operational Considerations

- **Queues:** no new queues/containers. `process_labeling_job` runs on `default`,
  `process_labeling_task` on `llm`. Priority table: urgent subs 4, urgent extractions / default
  subs 3, default extractions 2, **labeling 1**.
- **DB growth:** `LabelResult` ≈ reports × active labels (1M × 20 ≈ 20M); `GateAnswer` ≈ reports ×
  active groups. Indexes are declared on the initial migration (built while empty).
- **Logging:** `radis.labels` logger — INFO on job prep / scan decisions, WARNING on skips (empty
  body, no active labels) and partial failures, ERROR with traceback on LLM failure.
- **Observability:** live status in `LabelingJobAdmin`; `labels_status` management command
  (`uv run cli shell` + `labels_status`, or `manage.py labels_status`) reports corpus-wide
  label/gate/result counts and the scan checkpoint.
- **Docs:** `CLAUDE.md` (apps list, env vars, troubleshooting), `KNOWLEDGE.md` (prompt/label/gate
  authoring guidance), `example.env`.

## Testing Strategy

- **Unit (no DB):** prompt rendering substitutes `$report` and contains no label text; schema
  builders produce name-keyed fields validating the right value sets, with a drift guard against
  the model `TextChoices`.
- **Model/DB:** stale-detection predicates; backfill scope query across all gate/result freshness
  combinations; singleton index raises on a second active job; idempotent prep on retry; cascade +
  uniqueness; empty-scope job finishes `SUCCESS`.
- **Engine (LLM mocked):** gate batching; NO-gate skips the group; five-bucket storage with only
  surfacing buckets visible; all decision-table transitions incl. atomic `YES→NO` flip; skip
  conditions; per-report failure → task `WARNING`.
- **Scan:** first run, active-job guard, no-new-reports (checkpoint still advances), creates scan
  job, scope isolation, checkpoint singleton.
- **Admin:** backfill creates a MANUAL job; conflict is a no-op when active; group search; read-only
  admins block writes; duplicate group/label name rejected.
- **Search filter:** single-label match; OR across multiple; only surfacing buckets match; no
  selection applies no filter; form lists only active labels alphabetically and omits the field
  when none exist.
- **Surfacing:** badges render for surfacing buckets only.

## Risks and Open Questions

- **LLM cost on a 1M-report backfill** — gating helps, but a full backfill is a multi-day, costed
  operation; model cost before production.
- **Gate/label wording quality** — bad gate questions screen incorrectly; vague label descriptions
  yield inconsistent bucket boundaries. Authoring guidance in `KNOWLEDGE.md`; no automated
  validation in v1.
- **Report deletion blast radius** — cascades to `LabelResult`/`GateAnswer` (standard Django).

## Out of Scope (Future Work)

Manual override/correction; versioned history; per-label/group backfill from admin; a "stale"
search modifier; bucket-specific search syntax; LLM-call preview in admin; Prometheus metrics;
localized prompts; clickable label badges; grouping the filter widget by `LabelGroup`.
