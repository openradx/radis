# Auto-Labeling Feature — Design

**Status:** Approved design, ready for implementation planning
**Date:** 2026-05-21 (revised 2026-06-03)
**Owner:** Kai Schlamp

## Overview

Add an auto-labeling feature to RADIS that classifies radiology reports against admin-defined labels using an LLM. Labels are organised into `LabelGroup`s. Each group carries a **gate question** — a single upfront Yes/No/Maybe screening question asked before the group's labels. For each label the LLM assigns exactly one of five buckets: `PRESENT`, `LIKELY`, `POSSIBLE`, `ABSENT`, `UNMENTIONED`. The three "some evidence" buckets (`PRESENT`, `LIKELY`, `POSSIBLE`) attach the label to the report; `ABSENT` and `UNMENTIONED` are stored but hidden. Results are stored per `(report, label)` pair; gate answers per `(report, label_group)` pair.

The gate is intentionally a different value space from the labels. A gate question ("Is this a Head CT?") is a categorical *applicability* check — Yes/No/Maybe — answering "do this group's labels even apply to this report?". The five buckets answer a different question per label — "does this finding appear, and how strongly?" — where `UNMENTIONED` (the report never discusses the topic) is a genuinely distinct and useful state that a gate has no analogue for.

Two execution paths share the same labeling logic:

1. **Periodic scan path** — A Procrastinate periodic task runs on a configurable cron schedule. It finds reports created since the last scan tick and enqueues batched labeling tasks. If an admin-triggered backfill is active, the scan yields to the backfill for that tick without enqueuing anything and without advancing the checkpoint.
2. **Backfill path** — An admin-triggered `LabelingJob` walks the existing report corpus and produces missing or stale results. Used for the initial bulk upload and any future large-scale re-labeling (e.g. after a label update). Only one backfill may be active at a time.

Labels surface on the report detail page (badges) and in search (`label:` query filter + facet panel).

## Goals

- Admins can author label groups (with gate questions) and labels (name + description) in the Django admin and see them applied to reports automatically.
- New and updated reports are labeled in the background without blocking ingest.
- Existing reports (up to ~1M scale) can be backfilled.
- Gating reduces LLM calls by skipping irrelevant label groups entirely.
- Edits to a label or gate question naturally produce stale results, which the next backfill refreshes.
- End users see applied labels on each report and can filter search by label.

## Non-Goals (v1)

- Manual label editing by end users.
- Versioned result history (only the latest result per `(report, label)` is kept).
- Per-label backfill targeting (backfill is system-wide).
- A `label:foo:present` search syntax that filters by specific bucket (revisit if requested).
- Surfacing `ABSENT` or `UNMENTIONED` results anywhere user-facing.
- Performance dashboards beyond the live progress shown in the `LabelingJob` admin.
- Localized prompts — labels and reports can be in any language and are passed verbatim to the LLM.

## Data Model

A new Django app `radis.labels` with five models.

```python
class LabelGroup(models.Model):
    name          = models.CharField(max_length=100, unique=True)
    gate_question = models.TextField()        # upfront Yes/No/Maybe screening question for this group
    updated_at    = models.DateTimeField(auto_now=True)  # drives gate stale detection

    class Meta:
        ordering = ["name"]


class Label(models.Model):
    group       = models.ForeignKey(LabelGroup, on_delete=models.CASCADE,
                                    related_name="labels")
    name        = models.CharField(max_length=100)   # the label string that surfaces (e.g. "pneumonia")
    description = models.TextField()                  # definition sent to the LLM to classify this label
    active      = models.BooleanField(default=True)
    created_at  = models.DateTimeField(auto_now_add=True)
    updated_at  = models.DateTimeField(auto_now=True)  # drives result stale detection

    class Meta:
        constraints = [models.UniqueConstraint(fields=["name"], name="unique_label_name")]
        indexes     = [models.Index(fields=["active"])]


class LabelResult(models.Model):
    class Value(models.TextChoices):
        PRESENT     = "PRESENT",     "Present"
        LIKELY      = "LIKELY",      "Likely"
        POSSIBLE    = "POSSIBLE",    "Possible"
        ABSENT      = "ABSENT",      "Absent"
        UNMENTIONED = "UNMENTIONED", "Unmentioned"

    # Buckets that attach the label to the report / search.
    SURFACING_VALUES = (Value.PRESENT, Value.LIKELY, Value.POSSIBLE)

    report       = models.ForeignKey(Report, on_delete=models.CASCADE, related_name="label_results")
    label        = models.ForeignKey(Label, on_delete=models.CASCADE, related_name="results")
    value        = models.CharField(max_length=11, choices=Value.choices)
    generated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["report", "label"],
                                    name="unique_result_per_report_label"),
        ]
        indexes = [
            models.Index(fields=["label", "value"]),  # search facet lookups
            models.Index(fields=["report"]),          # report detail page render
        ]


class GateAnswer(models.Model):
    class Value(models.TextChoices):
        YES   = "YES",   "Yes"
        NO    = "NO",    "No"
        MAYBE = "MAYBE", "Maybe"

    report      = models.ForeignKey(Report, on_delete=models.CASCADE,
                                    related_name="gate_answers")
    label_group = models.ForeignKey(LabelGroup, on_delete=models.CASCADE,
                                    related_name="gate_answers")
    value       = models.CharField(max_length=5, choices=Value.choices)
    generated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["report", "label_group"],
                                    name="unique_gate_answer_per_report_group"),
        ]
        indexes = [
            models.Index(fields=["label_group", "value"]),
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

- **Result stale:** `LabelResult.generated_at < F("label__updated_at")`
- **Gate answer stale:** `GateAnswer.generated_at < F("label_group__updated_at")`

No snapshot columns, no `is_stale` flags. Both comparisons are pure joins over indexed columns.

### Buckets and surfacing

All five bucket values are **stored**. `ABSENT` and `UNMENTIONED` produce real `LabelResult` rows — exactly as the old design stored `NO` answers — so stale detection and the backfill's "needs work" predicate treat a label that came back `ABSENT`/`UNMENTIONED` as *done*, with no re-labeling churn. Surfacing (report badges and search) filters to `LabelResult.SURFACING_VALUES`. The control flow never branches on a label's bucket value; the LLM returns a bucket and we store it.

### Constraints

- `LabelGroup.name` is unique. Groups are the unit of gate authoring.
- `Label.name` is unique. Two labels sharing a name would be ambiguous.
- `(LabelResult.report, LabelResult.label)` is unique. Re-labeling replaces via `update_or_create`.
- `(GateAnswer.report, GateAnswer.label_group)` is unique. Re-evaluation replaces via `update_or_create`.
- Cascade deletes on all FKs. Deleting a `LabelGroup` cascades to its `Label`s and `GateAnswer`s; deleting a `Label` cascades to its `LabelResult`s. Admins are warned by Django's standard delete confirmation.

## Execution Path 1: Periodic Incremental Scan

Replaces the original signal-based per-report path. RADIS controls batching entirely — no dependency on the shape or cadence of any external ETL pipeline.

### Design rationale

The signal-based approach tied task granularity to the ETL pipeline's call size: a bulk ingest of 1,000 reports would produce 1,000 individual queue tasks. The periodic scan decouples these concerns: reports accumulate in the database via normal ingest, and RADIS labels them on its own schedule in controlled batches.

The scan's responsibility is narrow: label newly ingested reports. Finding reports with missing or stale results (e.g. after a label edit) is exclusively the backfill's job.

Both paths are symmetric: each creates a `LabelingJob` (distinguished by `trigger`) that flows through the same Job → Task machinery. This keeps the processing layer, admin visibility, priority handling, and singleton constraint identical for both.

### Trigger

A Procrastinate periodic task registered with `@app.periodic(cron=settings.LABELING_SCAN_CRON)`.

### Checkpoint

`LabelingScanCheckpoint` (single row, `pk=1`) tracks `last_scanned_at`. It defines the scan window — reports with `created_at >= last_scanned_at` are in scope for the next scan job. The checkpoint advances to the current tick's timestamp whenever the scan creates a job or finds no new reports to process. It does **not** advance when the tick is skipped because an active `LabelingJob` exists.

On the very first tick (`last_scanned_at is None`), the scan records the current timestamp and exits without creating a job. Existing reports at that point must be handled by a manual backfill.

### Guard: yield to active LabelingJob

When any `LabelingJob` is active (whether a prior scan job or a manual backfill), the scan returns immediately and **does not advance the checkpoint**. Once the active job completes, the next scan tick resumes from the unchanged checkpoint and covers all reports created since that point. Reports already labeled by a backfill are processed by `label_report` at zero LLM cost (all results fresh). The checkpoint then advances to the current tick time.

### Create scan job

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
        # First run: record this moment; existing reports belong to the manual backfill.
        checkpoint.last_scanned_at = now
        checkpoint.save()
        return

    if not Label.objects.filter(active=True).exists():
        return

    if Report.objects.filter(created_at__gte=checkpoint.last_scanned_at).exists():
        job = LabelingJob.objects.create(
            trigger=LabelingJob.Trigger.SCAN,
            scan_from=checkpoint.last_scanned_at,
        )
        job.delay()

    checkpoint.last_scanned_at = now
    checkpoint.save()
```

The periodic task's only responsibility is deciding whether to create a `LabelingJob` and advancing the checkpoint. All report iteration, task creation, and LLM work is handled by the shared Job → Task machinery. If there are no new reports the checkpoint still advances so the next tick doesn't re-query the same empty window.

### Scale

At ~100 reports/day with a daily cron schedule, each tick produces at most one `LabelingJob` containing one `LabelingTask`. No task explosion tied to ETL call size.

## Execution Path 2: Manual Backfill

Uses the same Job/Task machinery as the periodic scan. The only differences are the trigger value and the report scope (full corpus vs. recent window).

### Models

```python
class LabelingJob(AnalysisJob):
    class Trigger(models.TextChoices):
        SCAN   = "SCAN",   "Periodic scan"
        MANUAL = "MANUAL", "Manual backfill"

    trigger   = models.CharField(max_length=10, choices=Trigger.choices, default=Trigger.MANUAL)
    scan_from = models.DateTimeField(null=True, blank=True)
    # scan_from: set for SCAN jobs to the checkpoint timestamp at job creation time,
    # defining the report window (created_at >= scan_from). None for MANUAL jobs.

    default_priority = settings.LABELING_JOB_PRIORITY
    urgent_priority  = settings.LABELING_JOB_PRIORITY  # labeling is never urgent

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

`create_labeling_tasks_streaming` determines its report scope from `job.scan_from`:

- **SCAN job** (`scan_from` is set): `Report.objects.filter(created_at__gte=job.scan_from).order_by("pk")` — only newly ingested reports since the checkpoint.
- **MANUAL job** (`scan_from` is `None`): `_needs_work_queryset(active_group_count).order_by("pk")` — all reports with missing or stale results across the full corpus.

For each chunk it bulk-creates `LabelingTask` rows of `LABELING_TASK_BATCH_SIZE` reports each.

Django's `iterator(chunk_size=...)` opens a PostgreSQL server-side cursor, so the result set is frozen at the moment the cursor opens. Reports ingested after that point are invisible to the job — they are not added to its task list and do not extend its runtime. The periodic scan's checkpoint is held frozen while any `LabelingJob` is active (the guard returns without advancing it); once the job completes, the next scan tick resumes from the unchanged checkpoint and covers the full window including reports the prior job's cursor never saw.

The `tasks.all().delete()` at the top makes the preparation phase idempotent. If Procrastinate retries the task after a crash mid-streaming, any partial `LabelingTask` rows from the previous attempt are cleared before streaming restarts. The cost is bounded — only this job's own tasks are deleted, however many the prior attempt managed to create.

### "Needs work" predicate

Used exclusively by the backfill. A report needs work if **either** condition holds:

```python
def _needs_work_queryset(
    active_group_count: int,
) -> QuerySet:
    return (
        Report.objects.annotate(
            non_stale_gate_count=Count(
                "gate_answers",
                filter=Q(
                    gate_answers__label_group__labels__active=True,
                    gate_answers__generated_at__gte=F(
                        "gate_answers__label_group__updated_at"
                    ),
                ),
                distinct=True,
            ),
        )
        .filter(
            Q(non_stale_gate_count__lt=active_group_count)
            | Exists(
                GateAnswer.objects.filter(
                    report=OuterRef("pk"),
                    value__in=[GateAnswer.Value.YES, GateAnswer.Value.MAYBE],
                    generated_at__gte=F("label_group__updated_at"),
                ).filter(
                    Exists(
                        Label.objects.filter(
                            group_id=OuterRef("label_group_id"),
                            active=True,
                        ).exclude(
                            results__report_id=OuterRef(OuterRef("pk")),
                            results__generated_at__gte=F("updated_at"),
                        )
                    )
                )
            )
        )
    )
```

- **Condition A** — missing or stale gate answers for any active group (`non_stale_gate_count < active_group_count`).
- **Condition B** — there exists a fresh `YES`/`MAYBE` gate answer whose group contains at least one active label without a fresh `LabelResult` for this report. "Not fresh" means either no row at all, or `result.generated_at < label.updated_at` (label edited after the result was written). The inner `Exists` uses a double `OuterRef` — `OuterRef("label_group_id")` references the `GateAnswer` row; `OuterRef(OuterRef("pk"))` references the `Report.pk` two levels up.

`active_label_count` is no longer a parameter. NO-gated groups have no `LabelResult` rows by design, so comparing a flat total-result count to the total active label count would always flag those reports as needing work.

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

`label_report` is the single function used by both execution paths. It encapsulates the two-phase gate-then-label logic. Nothing in its control flow branches on a label's bucket value — the LLM returns a bucket per label and the result is stored as-is.

### Dynamic schema and generic prompt

Labels and groups are admin-authored at runtime, so the structured-output schema sent to the LLM is **generated on the fly** from the database rows; nothing about specific labels is hardcoded. The division of labour is:

- **The schema enforces the choice.** Each label/group becomes one required field whose type is a fixed enum. Structured output guarantees the model returns exactly one valid value per field. The enum types are **static** (the five buckets and the three gate values never change at runtime); only the *set of fields* is dynamic.
- **The prompt teaches the choice.** A static, generic prompt explains what each bucket/gate value *means* and carries the report body. It contains **no label-specific text** — the prompt is identical for every label and every report.
- **Each field's `description=` carries the label.** The label `name` + `description` (or the group's `gate_question`) go into the Pydantic `Field(description=...)`, which the LLM reads via the JSON schema to understand that field. This is the *only* place per-label content lives.

**Id-keyed fields are the contract between schema and database.** A field is keyed `label_<id>` / `group_<id>`, derived purely from the primary key. The human name and definition live in `description=` (model reading-material only) and never appear in the answer. The model returns `{"label_42": "PRESENT", ...}`; we strip the prefix to recover the FK — no name matching, immune to renames, spaces, unicode, or duplicate names.

The builders live in `radis/labels/utils/` (`schemas.py`, `prompts.py`) and are **owned by the labels app**. They import `pydantic.create_model` and `radis.chats.utils.chat_client.ChatClient` (the same edge extractions already uses) and import **nothing** from `radis.extractions` — the extractions app's `generate_output_fields_schema` is deliberately not reused, keeping the apps decoupled.

```python
# radis/labels/utils/schemas.py — static enums, dynamic fields.
class BucketValue(StrEnum):   # PRESENT, LIKELY, POSSIBLE, ABSENT, UNMENTIONED — mirrors LabelResult.Value
    ...
class GateValue(StrEnum):     # YES, NO, MAYBE — mirrors GateAnswer.Value
    ...

def build_label_classification_schema(labels: Sequence[Label]) -> type[BaseModel]:
    fields = {
        f"label_{lbl.id}": (BucketValue, Field(description=f"{lbl.name}: {lbl.description}"))
        for lbl in labels
    }
    return create_model("LabelClassification", **fields)

def build_gate_schema(groups: Sequence[LabelGroup]) -> type[BaseModel]:
    fields = {
        f"group_{g.id}": (GateValue, Field(description=g.gate_question))
        for g in groups
    }
    return create_model("GateScreening", **fields)

def parse_label_results(parsed: BaseModel) -> dict[int, str]:
    return {int(k.removeprefix("label_")): v for k, v in parsed.model_dump().items()}

def parse_gate_results(parsed: BaseModel) -> dict[int, str]:
    return {int(k.removeprefix("group_")): v for k, v in parsed.model_dump().items()}
```

```python
# radis/labels/utils/prompts.py — generic prompts, only $report substituted.
def render_label_prompt(report_body: str) -> str:
    return Template(settings.LABELING_SYSTEM_PROMPT).substitute(report=report_body)

def render_gate_prompt(report_body: str) -> str:
    return Template(settings.LABELING_GATE_SYSTEM_PROMPT).substitute(report=report_body)
```

A unit test asserts `BucketValue`/`GateValue` values equal `LabelResult.Value`/`GateAnswer.Value` (drift guard — the static enums must stay in sync with the model `TextChoices`). Every generated field is required (no default), so every label receives a bucket and every group a gate answer. Empty label/group subsets never reach the builders — `label_report` already guards against empty LLM calls, which also avoids a zero-field `create_model`.

```python
def label_report(report_id: int) -> None:
    report = Report.objects.get(id=report_id)
    if not report.body or not report.body.strip():
        return

    active_groups = (
        LabelGroup.objects
        .filter(labels__active=True)
        .prefetch_related("labels")
        .distinct()
    )
    if not active_groups:
        return

    client = ChatClient()

    # Load existing gate answers to classify groups by gate status
    existing_gates = {
        ga.label_group_id: ga
        for ga in GateAnswer.objects.filter(report=report, label_group__in=active_groups)
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
        prompt = render_gate_prompt(report.body)
        parsed = client.extract_data(prompt, schema)
        new_gate_results.update(parse_gate_results(parsed))  # {group_id: "YES"|"NO"|"MAYBE"}

    # Phase 2 — Process each group
    for group in active_groups:
        labels = [l for l in group.labels.all() if l.active]

        if group.id in new_gate_results:
            # Gate was re-evaluated this run
            new_value = new_gate_results[group.id]
            old_gate  = existing_gates.get(group.id)
            old_value = old_gate.value if old_gate else None

            with transaction.atomic():
                GateAnswer.objects.update_or_create(
                    report=report, label_group=group,
                    defaults={"value": new_value},
                )
                if new_value == GateAnswer.Value.NO and old_value in (GateAnswer.Value.YES, GateAnswer.Value.MAYBE):
                    LabelResult.objects.filter(report=report, label__group=group).delete()

            if new_value in (GateAnswer.Value.YES, GateAnswer.Value.MAYBE):
                labels_to_run = _get_stale_or_missing_labels(report, labels)
                if labels_to_run:
                    _run_label_set(client, report, labels_to_run)

        else:
            # Gate is fresh — use stored value, no gate LLM call
            gate_value = groups_with_fresh_gate[group.id]

            if gate_value in (GateAnswer.Value.YES, GateAnswer.Value.MAYBE):
                labels_to_run = _get_stale_or_missing_labels(report, labels)
                if labels_to_run:
                    _run_label_set(client, report, labels_to_run)
            # else: gate = NO, fresh — skip group entirely
```

`_run_label_set` builds a dynamic schema for the stale/missing labels via `build_label_classification_schema` (each label's `name` + `description` carried in its field `description=`), renders the generic label prompt, sends both to the LLM in one call, then maps the response back to label ids via `parse_label_results` and stores the returned bucket per label via `update_or_create`:

```python
def _run_label_set(client: ChatClient, report: Report, labels: list[Label]) -> None:
    schema = build_label_classification_schema(labels)
    parsed = client.extract_data(render_label_prompt(report.body), schema)
    for label_id, bucket in parse_label_results(parsed).items():
        LabelResult.objects.update_or_create(
            report=report, label_id=label_id, defaults={"value": bucket},
        )
```

### `_get_stale_or_missing_labels`

```python
def _get_stale_or_missing_labels(
    report: Report,
    labels: list[Label],
) -> list[Label]:
    fresh_ids = set(
        LabelResult.objects.filter(
            report=report,
            label_id__in=[l.id for l in labels],
            generated_at__gte=F("label__updated_at"),
        ).values_list("label_id", flat=True)
    )
    return [l for l in labels if l.id not in fresh_ids]
```

One DB query that simultaneously answers "should we run?" (non-empty result) and "what to run?" (the list itself). Only labels whose `LabelResult` is missing or whose `result.generated_at < label.updated_at` are returned. A label that previously came back `ABSENT`/`UNMENTIONED` still has a fresh result row, so it is naturally excluded. When the gate answer was previously `NO` or absent, there are no result rows for the group, so all labels are returned naturally.

### Decision table

*Gate answer re-evaluated (stale or no prior gate answer):*

| Gate answer was | Gate answer is now | Action |
|---|---|---|
| YES/MAYBE | YES/MAYBE | Save gate answer + run stale/missing labels (may be zero) |
| YES/MAYBE | NO | Save gate answer + delete all results for group |
| NO | YES/MAYBE | Save gate answer + run all labels (none have results) |
| NO | NO | Save gate answer *(no results to delete)* |
| no prior answer | YES/MAYBE | Save gate answer + run all labels (none have results) |
| no prior answer | NO | Save gate answer *(no results to delete)* |

*Gate answer fresh (no re-evaluation):*

| Gate answer value | Action |
|---|---|
| YES/MAYBE | Run stale/missing labels (skip group if all fresh) |
| NO | Skip group entirely |

`GateAnswer.update_or_create` is called only when the gate answer is re-evaluated (stale or no prior answer); it bumps `generated_at` even when the value is unchanged. When the gate flips to `NO`, the gate save and the result deletion are wrapped in `transaction.atomic()` so the two operations are all-or-nothing. If the deletion were to happen outside the transaction and the process crashed after the gate save, the orphaned results would never be detected by condition B of the "needs work" predicate (gate is `NO`, so that `Exists` subquery skips it), leaving them stuck indefinitely. The transaction prevents that inconsistency.

The label-result column is omitted from the re-evaluated table because `_get_stale_or_missing_labels` handles it uniformly: only labels without a fresh result are passed to the LLM, regardless of how many that is or what bucket they previously held. The caller does not need to know the prior result state to decide what to run.

### LLM call count (20 groups, gate batch 10, 3 pass gate, 4 labels per group)

"Group call" here means one LLM call containing only the stale/missing labels for that group — not the full label set.

| Scenario | Without gating | With gating |
|---|---|---|
| First labeling (all fresh) | 20 | 2 gate + 3 group calls (4 labels each) = **5** |
| Re-run, 1 label stale in 1 group (gate fresh) | 20 | 0 gate + **1 label** in 1 call = **1** |
| Re-run, gate text changed (still YES, results fresh) | 20 | 2 gate + 0 group = **2** |
| Re-run, gate flips NO→YES (no prior results) | 20 | 2 gate + 3 group calls (4 labels each) = **5** |

### Skip conditions

- `report.body` empty/whitespace.
- No active labels / no active groups.

### Failure handling

Per-report exceptions are caught inside `LabelingTaskProcessor`; one report's LLM failure does not abort the rest of the batch. Failures are logged at `ERROR` level with full traceback. A report that fails in a scan job will not be re-attempted by the next scan (the checkpoint will have advanced past its `created_at`); the next manual backfill will catch it via the missing-results predicate. Procrastinate retries the `process_labeling_task` job itself only on uncaught exceptions (i.e. infrastructure failures outside the per-report loop).

## Admin UX

### `LabelGroupAdmin`

Primary surface for authoring groups and their gate questions.

```python
class LabelGroupAdmin(admin.ModelAdmin):
    list_display    = ("name", "gate_question_preview", "active_label_count", "updated_at")
    search_fields   = ("name",)   # required for autocomplete in LabelAdmin
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
- Admins must create a `LabelGroup` before adding labels to it; the gate question is a required part of the group.

### `LabelAdmin`

`group` is a proper FK autocomplete:

```python
class LabelAdmin(admin.ModelAdmin):
    autocomplete_fields = ["group"]
    list_display        = ("name", "group", "active", "description_preview", "updated_at", "result_summary")
    list_filter         = ("active", "group")
    search_fields       = ("name", "group__name", "description")
    ordering            = ("group__name", "name")
    readonly_fields     = ("created_at", "updated_at", "result_summary")
    fieldsets = (
        (None,    {"fields": ("group", "name", "description", "active")}),
        ("Stats", {"fields": ("result_summary", "created_at", "updated_at")}),
    )
```

- `result_summary` shows live 5-bucket counts: `120 Present · 30 Likely · 15 Possible · 200 Absent · 9,000 Unmentioned · 12 stale`.

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

The backfill cockpit with Run/Cancel banner and live progress panel. `trigger` is added to `list_display` so admins can distinguish nightly scan jobs from manually triggered backfills at a glance. Scan-triggered jobs (`trigger=SCAN`) are created automatically and cannot be initiated from the admin form; only `MANUAL` jobs expose the Run button.

### `LabelResultAdmin`

Read-only ops/debugging view.

```python
class LabelResultAdmin(admin.ModelAdmin):
    list_display    = ("report", "label", "value", "is_stale", "generated_at")
    list_filter     = ("value", "label")   # value filter exposes all 5 buckets
    search_fields   = ("report__document_id", "label__name")
    raw_id_fields   = ("report", "label")
    readonly_fields = tuple(f.name for f in LabelResult._meta.fields)

    def has_add_permission(self, request):    return False
    def has_change_permission(self, *a):      return False
```

`is_stale` annotated via `F("label__updated_at") > F("generated_at")`.

### `GateAnswerAdmin`

Read-only, mirrors `LabelResultAdmin`:

```python
class GateAnswerAdmin(admin.ModelAdmin):
    list_display    = ("report", "label_group", "value", "is_stale", "generated_at")
    list_filter     = ("value", "label_group")
    search_fields   = ("report__document_id", "label_group__name")
    raw_id_fields   = ("report", "label_group")
    readonly_fields = tuple(f.name for f in GateAnswer._meta.fields)

    def has_add_permission(self, request):    return False
    def has_change_permission(self, *a):      return False
```

`is_stale` annotated via `F("label_group__updated_at") > F("generated_at")`.

### `ReportAdmin` inline

`LabelResultInline` on the Report change form shows per-label results.

### Intentionally omitted (v1)

- "Preview LLM call" button on `LabelGroupAdmin`.
- Bulk "mark stale" action.
- Per-label backfill.

## End-User Surfacing

### Report detail page

Cotton component `<c-report-labels :report="report" />` renders labels whose `LabelResult.value IN (PRESENT, LIKELY, POSSIBLE)`, grouped by `label_group.name`. All three surfacing buckets render an **identical badge**; the exact bucket is shown on hover/tooltip. `ABSENT` and `UNMENTIONED` results are not shown. Stale results are shown with a muted style and tooltip.

### Search filter (`label:`)

`label:pneumonia` filters by `label.name` and `value IN (PRESENT, LIKELY, POSSIBLE)`. Facet panel lists top N label names with counts over those three buckets.

## Settings

Added to `radis/settings/base.py`:

```python
# Priorities (Procrastinate uses higher = sooner).
# Existing baseline:
#   EXTRACTION_DEFAULT_PRIORITY = 2 · EXTRACTION_URGENT_PRIORITY = 3
#   SUBSCRIPTION_DEFAULT_PRIORITY = 3 · SUBSCRIPTION_URGENT_PRIORITY = 4
# Scan and manual backfill share one priority: only one LabelingJob runs at a time
# so relative priority between them is meaningless.
LABELING_JOB_PRIORITY = env.int("LABELING_JOB_PRIORITY", default=1)

LABELING_TASK_BATCH_SIZE       = env.int("LABELING_TASK_BATCH_SIZE",       default=100)
LABELING_LLM_CONCURRENCY_LIMIT = env.int("LABELING_LLM_CONCURRENCY_LIMIT", default=6)
LABELING_GATE_BATCH_SIZE       = env.int("LABELING_GATE_BATCH_SIZE",       default=10)

# Cron schedule for the periodic incremental scan (default: daily at 2 AM).
LABELING_SCAN_CRON = env.str("LABELING_SCAN_CRON", default="0 2 * * *")

LABELING_SYSTEM_PROMPT      = env.str("LABELING_SYSTEM_PROMPT",      default=DEFAULT_LABELING_SYSTEM_PROMPT)
LABELING_GATE_SYSTEM_PROMPT = env.str("LABELING_GATE_SYSTEM_PROMPT", default=DEFAULT_GATE_SYSTEM_PROMPT)
```

Both prompts are **generic** — they carry no label- or group-specific text. Each label/group is a field in the dynamically generated schema, and its name/description (or gate question) rides in that field's `description=`. The prompt only teaches what the enum values mean and supplies the report body, so the single substituted placeholder is `$report`.

Default label prompt:

```text
You are an AI medical assistant. The provided schema lists one field per label, each
field's description defining the label. For every field, decide how strongly the report
below supports that label, choosing exactly one of:
  - "PRESENT"     — the report clearly states this is present
  - "LIKELY"      — the report strongly suggests it, without stating it outright
  - "POSSIBLE"    — the report leaves it as a possibility / cannot be excluded
  - "ABSENT"      — the report explicitly states this is not present
  - "UNMENTIONED" — the report does not address this at all

Return answers in JSON format matching the provided schema.

Radiology Report:
$report
```

Default gate prompt (a Yes/No/Maybe applicability screen):

```text
You are an AI medical assistant. The provided schema lists one field per topic, each
field's description stating the topic's screening question. For every field, answer
whether the radiology report below contains content relevant to that topic, responding
with exactly one of:
  - "YES"   — the report clearly contains relevant content
  - "NO"    — the report clearly does not
  - "MAYBE" — the report may contain relevant content; use when uncertain

Return answers in JSON format matching the provided schema.

Radiology Report:
$report
```

## Operational Considerations

### Worker / queue topology

No new queues or containers. Priority table updated (scan and manual backfill share one priority since the singleton constraint prevents them from competing):

| Source | Priority |
|---|---|
| Urgent subscriptions | 4 |
| Urgent extractions / default subs | 3 |
| Default extractions | 2 |
| Labeling (scan and manual backfill) | 1 |

### Database growth

- `LabelResult` table: reports × active labels (1M × 20 ≈ 20M rows).
- `GateAnswer` table: reports × active groups (1M × 5 ≈ 5M rows, assuming ~4 labels per group averages to ~5 groups).
- Indexes declared on initial migrations (built while tables are empty).

### Logging

`radis.labels` logger:
- `INFO` on batch task start/finish (batch size + duration).
- `INFO` on gate phase: groups evaluated, groups skipped (gate fresh).
- `WARNING` on skip (empty body, no active labels).
- `ERROR` with full traceback on LLM failure after retries.

### Observability

- Live progress in `LabelingJobAdmin` change form.
- Management command `uv run cli labels-status`: last scan checkpoint timestamp (or "never"), total reports, fully-current count, missing/stale count, per-label Present/Likely/Possible/Absent/Unmentioned/stale counts, per-group gate Yes/Maybe/No/stale counts.
- No Prometheus metrics in v1.

### Documentation updates

- `CLAUDE.md`: add `radis.labels` to Django Apps list; add the seven new env vars; add Troubleshooting subsection.
- `KNOWLEDGE.md`: document prompt design, label authoring guidelines (label description should be self-contained and define the finding precisely), and gate-question authoring guidelines (gate question is an applicability screen answerable from the report body; phrase as a topic-level Yes/No/Maybe screen, not a specific finding).
- `example.env`: seven new env vars with comments and defaults.

### Existing-system touchpoints

1. `radis/reports/admin.py` — add `LabelResultInline` to `ReportAdmin.inlines`.
2. `radis/search/parser.py` — register `label:` as a known field filter.
3. `radis/pgsearch/` — translator for the `label:` AST node.
4. `radis/settings/base.py` — new settings block (additive).
5. Templates — Cotton component on report detail and search facet panel.

No touch to `radis/reports/site.py` — the periodic scan does not use the reports created/updated handler extension points.

## Testing Strategy

### Unit tests (no DB)

- **Prompt rendering** (`test_prompts.py`) — `render_label_prompt` and `render_gate_prompt` substitute `$report` (incl. unicode content) and contain the bucket/gate value meanings; assert **no label name or description appears in the prompt** (per-label content belongs only in the schema).
- **Schema building** (`test_schemas.py`) — `build_label_classification_schema` produces a Pydantic model whose fields are keyed `label_<id>`, each accepting only the five bucket values and rejecting unknown buckets / missing fields, with the label `name` + `description` in the field `description`; `build_gate_schema` produces `group_<id>` fields validating Yes/No/Maybe and rejecting unknown values, with the gate question in the field `description`; `parse_label_results` / `parse_gate_results` round-trip the prefixed keys back to integer ids; a **drift guard** asserts `BucketValue` / `GateValue` values equal `LabelResult.Value` / `GateAnswer.Value`.

### Model / DB tests

- **Stale detection** (`test_stale_detection.py`) — assert result and gate-answer stale predicates over hand-built rows.
- **Backfill scope query** (`test_scope.py`) — reports covering all combinations of gate-answer freshness and YES/MAYBE-gate answer freshness; a report with a fresh NO gate and no results is not included; a report with a fresh YES gate but a stale result is included; a report whose only results are `ABSENT`/`UNMENTIONED` but fresh is not included; the scope query returns exactly those needing work.
- **Singleton backfill** (`test_singleton.py`) — second concurrent active `LabelingJob` raises `IntegrityError`.
- **Backfill restart safety** (`test_singleton.py`) — if `process_labeling_job` is called twice for the same job (simulating a Procrastinate retry), partial `LabelingTask` rows from the first call are deleted before the second call recreates them; no duplicates result.
- **Cascade and uniqueness** (`test_models.py`) — group delete cascades to labels and gate answers; label delete cascades to results; unique constraints enforced; upserts work.

### Integration tests (LLM mocked)

- **Gate batching** (`test_gate.py`) — 20 groups with gate batch size 10 → exactly 2 gate LLM calls; only YES/MAYBE groups produce label-set calls.
- **Gate = NO, fresh** (`test_gate.py`) — group is skipped entirely; no `LabelResult` rows written, no LLM call.
- **Bucket storage and surfacing** (`test_buckets.py`) — a label-set call returning each of the five buckets writes a `LabelResult` for all of them; only PRESENT/LIKELY/POSSIBLE surface in badges and `label:` search, while ABSENT/UNMENTIONED are stored but hidden.
- **Gate answer fresh + YES/MAYBE + results all fresh** → zero LLM calls.
- **Gate answer fresh + YES/MAYBE + 1 label stale** → 1 LLM call containing only that 1 label, 0 gate calls.
- **Gate answer stale + new YES, old NO** → 1 gate call + 1 label-set call (all labels, no prior results).
- **Gate answer stale + new YES, old YES + results fresh** → 1 gate call only, 0 label calls.
- **Gate stale + new NO, old YES/MAYBE** → 1 gate call, existing results deleted, no label-set call.
- **Gate stale + new NO, old NO** → 1 gate call, no results to delete, no label-set call.
- **Gate flips YES/MAYBE → NO: atomicity** (`test_gate.py`) — gate save and result deletion succeed together; no orphaned `LabelResult` rows exist after the call.
- **Skip conditions** (`test_skips.py`) — empty body / no active labels → no LLM call.
- **Backfill task processor** (`test_processor.py`) — partial failure on one report yields task `WARNING`; successful reports still have results written.
- **Incremental scan: first run** (`test_scan.py`) — null checkpoint → no `LabelingJob` created, checkpoint set to `now`.
- **Incremental scan: guard** (`test_scan.py`) — active `LabelingJob` → no job created, checkpoint unchanged.
- **Incremental scan: no new reports** (`test_scan.py`) — checkpoint set, no reports after it → no `LabelingJob` created, checkpoint still advances.
- **Incremental scan: creates scan job** (`test_scan.py`) — reports exist after checkpoint → one `LabelingJob(trigger=SCAN, scan_from=checkpoint)` created and `delay()` called; checkpoint advances to `now`.
- **Incremental scan: scope isolation** (`test_scan.py`) — scan job's `create_labeling_tasks_streaming` only includes reports with `created_at >= scan_from`; older reports produce no tasks.
- **Incremental scan: checkpoint singleton** (`test_scan.py`) — calling `save()` twice on separate `LabelingScanCheckpoint` instances does not create a second row; `has_add_permission` returns `False` in admin.

### Admin tests

- **`LabelGroupAdmin`** — duplicate name surfaces as form error; `updated_at` bumps on save.
- **`LabelAdmin`** — autocomplete on group FK works; duplicate label name surfaces as form error.
- **`LabelingJobAdmin`** — Run/conflict/Cancel flows unchanged.

### Search integration tests

`label:pneumonia` returns reports with a surfacing-bucket result for that label and excludes reports whose only `pneumonia` result is `ABSENT`/`UNMENTIONED`.

### UI tests (Playwright, `@pytest.mark.acceptance`)

Badges render for surfacing buckets only; tooltip exposes the exact bucket; stale results show muted styling.

### One real-LLM acceptance test (`@pytest.mark.acceptance`, `cpu` profile)

- Create one group ("Lung findings", gate: "Does this report contain lung-related findings?"), one label ("clear chest", description "The lungs are clear with no abnormality."), ingest a synthetic report stating "No abnormalities, lungs clear.", trigger `incremental_label_scan` directly, assert a `GateAnswer(value=YES)` and a `LabelResult` in a surfacing bucket (`PRESENT`/`LIKELY`/`POSSIBLE`) appear.

### Out of scope (test-wise)

- Performance / load tests on 1M-row backfill.
- Procrastinate's retry semantics.
- Migrations on existing data — all tables start empty.

## Risks and Open Questions

- **LLM cost on the 1M-report backfill.** Gating reduces per-report cost significantly, but a full backfill is still a multi-day operation. Cost should be modeled before turning on for production.
- **Gate question wording quality.** A poorly worded gate question can screen out reports incorrectly. Admins are expected to author gate questions carefully; no safeguards in v1.
- **Label description quality.** Bucket assignments are only as good as the label descriptions; vague descriptions yield inconsistent PRESENT/LIKELY/POSSIBLE boundaries. Authoring guidance lives in `KNOWLEDGE.md`; no automated validation in v1.
- **`label:` token in user queries.** If free-text search contains `label:`, the parser treats it as a field filter. Matches behavior of other field filters; acceptable.
- **Report deletion blast radius.** Cascade to `LabelResult` and `GateAnswer` rows. Standard Django behavior.

## Out of Scope (Future Work)

- Manual override / user-correction of labels.
- Versioned result history.
- Per-label or per-group backfill triggered from admin.
- A "stale" search filter modifier.
- A `label:foo:present` bucket-specific search syntax.
- LLM call preview in the label group admin.
- Prometheus metrics on labeling throughput.
- Localized prompts.
