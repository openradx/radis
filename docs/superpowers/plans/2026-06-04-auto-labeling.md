# Auto-Labeling — Implementation Plan (consolidated)

**Date:** 2026-06-04 (consolidated 2026-06-10)
**Spec:** `docs/superpowers/specs/2026-05-21-auto-labeling-design.md`

> Single consolidated implementation plan for the auto-labeling feature. It reflects the
> final shipped design — name-keyed LLM schema fields, a YES/NO-only group gate, and the
> search-page **Filters** widget (not a typed `label:` syntax) — and supersedes the earlier
> per-phase plans (foundation / execution-admin / surfacing-observability / name-keyed-schema /
> label-search-filter), which have been squashed into this document.

## Architecture

`radis.labels` reuses the core Job → Task pattern: `LabelingJob` subclasses `AnalysisJob`,
`LabelingTask` subclasses `AnalysisTask`. A `trigger` field distinguishes `SCAN` (recent window,
owner-less) from `MANUAL` (full corpus). `process_labeling_job` (default queue) streams reports
into batched tasks during PREPARING; `process_labeling_task` (`llm` queue) runs `label_report` per
report under a thread pool. An `@app.periodic` task advances a singleton checkpoint and creates
SCAN jobs. A DB-level partial unique index enforces "one active LabelingJob at a time."

The single core function `label_report(report_id)` does the gate-then-label work; both execution
paths call it. The LLM schema is generated dynamically from DB rows with **name-keyed** fields; the
prompts are generic (only `$report` substituted).

## Task 1 — App scaffold, settings, data model, migration

Files: `radis/labels/{__init__,apps}.py`, `radis/labels/models.py`,
`radis/labels/migrations/0001_initial.py`, `radis/labels/factories.py`,
`radis/settings/base.py`, `example.env`.

- Register `radis.labels.apps.LabelsConfig` in `INSTALLED_APPS`.
- Models per spec: `LabelGroup`, `Label`, `LabelResult` (5-bucket `Value`, `SURFACING_VALUES`),
  `GateAnswer` (YES/NO), `LabelingScanCheckpoint` (pk-pinned singleton), `LabelingJob`
  (`Trigger` SCAN/MANUAL, nullable `owner`, `scan_from`, `ACTIVE_STATUSES`, no-op
  `_send_job_finished_mail`, `delay()` deferring `process_labeling_job`), `LabelingTask`
  (`reports` M2M, `delay()` deferring `process_labeling_task`).
- **Singleton index** — append a `RunSQL` to `0001_initial` (Django's `UniqueConstraint` can't
  express a constant-expression partial index):

  ```sql
  CREATE UNIQUE INDEX one_active_labeling_job
  ON labels_labelingjob ((true))
  WHERE status IN ('UV', 'PR', 'PE', 'IP', 'CI');
  ```

  with `reverse_sql="DROP INDEX IF EXISTS one_active_labeling_job;"`. Status codes are the DB
  values of `ACTIVE_STATUSES` (verify against `core/models.py`). The feature is pre-production, so
  this lives in the single initial migration rather than a follow-up.
- Settings block (all `env`-backed): `LABELING_JOB_PRIORITY=1`, `LABELING_TASK_BATCH_SIZE=100`,
  `LABELING_LLM_CONCURRENCY_LIMIT=6`, `LABELING_GATE_BATCH_SIZE=10`,
  `LABELING_SCAN_CRON="0 2 * * *"`, plus `LABELING_SYSTEM_PROMPT` / `LABELING_GATE_SYSTEM_PROMPT`
  with built-in generic defaults. Mirror the env vars (commented) in `example.env`.
- Factories for all models (`LabelingJobFactory` defaults to MANUAL + a `UserFactory` owner).

## Task 2 — Dynamic schema + generic prompts

Files: `radis/labels/utils/schemas.py`, `radis/labels/utils/prompts.py`.

- `BucketValue` / `GateValue` `StrEnum`s mirroring the model `TextChoices`.
- `build_label_classification_schema(labels)` and `build_gate_schema(groups)` — `create_model`
  with one **name-keyed** required field each (`lbl.name` / `g.name`), description =
  `lbl.description` / `g.gate_question`.
- `render_label_prompt` / `render_gate_prompt` — `Template(settings.…).substitute(report=body)`.
- Import only `pydantic.create_model` and `radis.chats…ChatClient`; nothing from
  `radis.extractions`.

## Task 3 — Core engine

Files: `radis/labels/labeling.py`, `radis/labels/scope.py`.

- `label_report(report_id)`: skip empty body / no active groups; load existing gates; re-gate only
  missing/stale groups in `LABELING_GATE_BATCH_SIZE` batches; per group apply the decision table
  (atomic `YES→NO` flip deletes the group's results); `YES` runs only stale/missing labels via
  `_run_label_set`; map LLM response back to ids inline; `update_or_create` per result.
- `_get_stale_or_missing_labels` — one query returning labels with missing/stale results.
- `_needs_work_queryset(active_group_count)` (`scope.py`) — condition A (missing/stale gate) OR
  condition B (fresh YES group with a missing/stale active-label result; double `OuterRef`).

## Task 4 — Orchestration

File: `radis/labels/tasks.py`.

- `process_labeling_job` (default queue): bail unless `PENDING`/`PREPARING`; idempotent
  `tasks.all().delete()`; preserve `started_at`; stream scope (`_scope_queryset`: SCAN window vs
  `_needs_work_queryset`) into `LABELING_TASK_BATCH_SIZE` tasks via chunked iterator; empty scope →
  finish `SUCCESS`; else `PENDING` and `delay()` each pending task.
- `process_labeling_task` (`llm` queue): run `LabelingTaskProcessor(task).start()`, clear
  `queued_job_id`.
- `incremental_label_scan` (`@app.periodic(cron=LABELING_SCAN_CRON)`): active-job guard (no
  advance) → first-run seed → no-active-labels (no advance) → create SCAN job when reports exist →
  advance checkpoint.

File: `radis/labels/processors.py`.

- `LabelingTaskProcessor(AnalysisTaskProcessor)`: `ThreadPoolExecutor(max_workers=
  LABELING_LLM_CONCURRENCY_LIMIT)` over the task's reports; `_safe_label` catches/logs per-report
  failures and downgrades the task to `WARNING`; `db.close_old_connections()` per thread and at
  end.

## Task 5 — Admin + management command

Files: `radis/labels/admin.py`,
`radis/labels/templates/admin/labels/labelingjob/change_list.html`,
`radis/labels/management/commands/labels_status.py`, `radis/reports/admin.py`.

- Authoring admins (`LabelGroupAdmin`, `LabelAdmin` with autocomplete + search).
- Read-only admins (`_ReadOnlyAdmin` base) for `LabelResult`, `GateAnswer`, `LabelingTask`;
  `is_stale` display columns; read-only `LabelingScanCheckpointAdmin`.
- `LabelingJobAdmin`: read-only, `has_add_permission=False`, custom changelist template with a
  **"Run backfill now"** button → `run_backfill_view` creates a MANUAL `PENDING` job inside
  `transaction.atomic()` + `except IntegrityError` (friendly "already active" message on conflict),
  then `delay()`.
- `LabelResultInline` (read-only) on `ReportAdmin`.
- `labels_status` command: corpus-wide label/gate/result counts and the scan checkpoint.

## Task 6 — Surfacing: report badges + search Filters widget

Files: `radis/reports/models.py`, `radis/reports/templates/reports/report_detail.html`,
`radis/labels/templates/cotton/report_labels.html`, `radis/search/forms.py`,
`radis/search/site.py`, `radis/search/views.py`, `radis/pgsearch/providers.py`,
`radis/search/utils/query_parser.py`.

- `Report.surfacing_label_results` cached property; render via the `report_labels` cotton component
  on the detail page (badges grouped by group, surfacing buckets only).
- **Filters widget (replaces typed `label:` syntax):** `SearchForm.labels =
  MultipleChoiceField(required=False)` populated from active labels (alphabetical, `size=6`);
  include in the layout only when active labels exist. `SearchFilters.labels: list[str]`;
  `SearchView` threads it through. `_build_filter_query` adds a single
  `Q(report__in=<surfacing subquery, value__in=SURFACING_VALUES, label__name__in=labels>)` — OR
  semantics, deduplicated. No `label:` parser registration.
- Drive-by: `query_parser.py` `quoteChar` → `quote_char` (clears pyparsing deprecation warnings).

## Test plan

`radis/labels/tests/` — `test_models.py`, `test_jobs.py` (singleton index, retry idempotency,
no-wipe-on-refire, empty-scope SUCCESS, nullable owner, mail no-op, scope), `test_admin.py`
(backfill button, conflict no-op, read-only admins, duplicate name), `test_labeling.py` (gate flow
+ decision table + atomic flip), `test_scope.py`, `test_scan.py`, `test_stale_detection.py`,
`test_processor.py` (partial failure → WARNING), `test_surfacing.py`, `test_search_filter.py`,
`test_labels_status.py`, and `unit/{test_schemas,test_prompts}.py`. Search-side label cases in
`radis/search/tests/{test_forms,test_views}.py`.

## Verification

```bash
uv run cli test -- radis/labels/            # full labels suite
uv run cli test                             # whole project, expect 0 warnings
# migrations consistent with model state:
uv run python manage.py makemigrations labels --check --dry-run   # "No changes detected"
```

## Documentation

Update `CLAUDE.md` (apps list, `LABELING_*` env vars, "Labels Not Appearing" troubleshooting that
references the Filters-panel widget rather than a `label:` token), `KNOWLEDGE.md` (label/gate
authoring guidance), and `example.env`.
