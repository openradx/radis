# Re-label Reports When Their Content Changes — Design

**Date:** 2026-08-09
**Status:** Approved
**Scope:** `radis.labels` (with read-only dependency on `radis.reports.models.Report.updated_at`)

## Problem

The labeling pipeline only ever considers a report once, keyed off `created_at`:

- The nightly incremental scan (`radis/labels/tasks.py`) finds candidate reports with
  `created_at__gte` — both the existence check in `incremental_label_scan` and the SCAN-job
  scope in `_scope_queryset`.
- Staleness of stored results is judged only against the *label side*: a `GateAnswer` is
  fresh if `generated_at >= label_group.updated_at`, a `LabelResult` if
  `generated_at >= label.updated_at`. The report's own `updated_at` is never consulted.

Consequence: a report whose body is edited after initial labeling (single-report API PUT or
bulk upsert — both bump `Report.updated_at`) is never re-labeled. Its stored gate answers
and label results silently describe the old body. Switching only the scan window to
`updated_at` would not fix this: the report would enter the scan job's scope but
`label_report()` would skip all work because its results still look fresh.

## Decision

Key both the scan window and staleness on `report.updated_at`. A result is fresh only if it
was generated after **both** the label's and the report's last change.

The body-hash alternative (store a hash of `report.body` on each result; re-label only on
actual body change) is **explicitly deferred**. It would make no-op upserts and
metadata-only edits free, at the cost of schema changes. Revisit if re-labeling cost
becomes a problem.

## Changes

### 1. Scan window — `radis/labels/tasks.py`

- `_scope_queryset`: SCAN jobs filter `updated_at__gte=job.scan_from` instead of
  `created_at__gte`.
- `incremental_label_scan`: the "anything new since the checkpoint?" existence check
  switches to `updated_at__gte=checkpoint.last_scanned_at`.

### 2. Staleness — report side added to every freshness predicate

- `radis/labels/labeling.py`
  - Gate freshness (in `label_report`): a gate answer is stale if
    `generated_at < group.updated_at` **or** `generated_at < report.updated_at`.
  - `_get_stale_or_missing_labels`: fresh requires `generated_at >= label.updated_at`
    **and** `generated_at >= report.updated_at` (second condition added to the query).
- `radis/labels/scope.py` — `_needs_work_queryset` (drives MANUAL backfills): all three
  freshness predicates gain `generated_at >= report.updated_at`:
  - `fresh_result_for_report`
  - `fresh_yes_gate`
  - the `non_stale_gate_count` annotation filter
- Consistency updates so tooling agrees with the engine:
  - `radis/labels/admin.py`: both `is_stale` displays (LabelResult, GateAnswer) add the
    report comparison.
  - `radis/labels/management/commands/labels_status.py`: stale counts add the report
    comparison.

No schema changes and no migrations: `Report.updated_at`, `GateAnswer.generated_at`, and
`LabelResult.generated_at` already exist.

## Behavioral Consequences (accepted trade-offs)

- Any report save — including metadata-only edits and no-op bulk re-pushes (the bulk upsert
  sets `updated_at = now` unconditionally) — makes all of that report's results stale and
  triggers full re-labeling (gate + labels) on the next scan tick or backfill. This is the
  accepted LLM cost of skipping the hash for now.
- A body edit re-runs the group gate, so a report can move YES→NO (existing logic then
  deletes the group's label results) or NO→YES.
- Backfills pick up edited reports too: `_needs_work_queryset` now includes reports whose
  results predate their last update.

## Known Limitation (documented, not handled)

If a report is updated *while* `label_report()` is running on it, the new results get a
`generated_at` later than that mid-flight `updated_at`, so the edit looks already-covered
and is missed until the report's next update. The window is seconds; fixing it requires the
deferred hash approach. Accepted.

## Testing

Unit tests per change site, plus a skip-path check:

1. A report updated after the checkpoint (but created before it) enters the SCAN job scope;
   an untouched old report does not.
2. A report whose `updated_at` postdates its results enters `_needs_work_queryset`; a
   report with results fresher than both label and report timestamps does not.
3. In `label_report`, a report updated after its gate answer gets the gate re-asked; a
   report updated after its label results gets those labels re-run.
4. Admin `is_stale` and `labels_status` stale counts reflect report-side staleness.
