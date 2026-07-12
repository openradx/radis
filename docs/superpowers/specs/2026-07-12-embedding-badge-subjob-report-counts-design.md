# Embedding pipeline badge: per-subjob report counts

**Date:** 2026-07-12
**Status:** Approved

## Problem

The embedding pipeline badge on the `ReportSearchIndex` admin changelist mixes two
axes without labeling them: "3121 reports awaiting embedding · 2 queued · 4
in-flight · 0 failed". The first number counts *reports* (`embedding IS NULL`);
the other three count *Procrastinate subjobs*. An operator cannot tell how many
reports the active subjobs actually cover — e.g. whether the 6 active subjobs
account for all 3121 pending reports or only some of them (reports with no queued
job exist after retry exhaustion or a cancelled backfill).

## Change

### Stats query — `_embedding_pipeline_stats` (`radis/pgsearch/admin.py`)

Extend the existing per-status aggregation over the `embeddings` queue to also sum
each job's report count, computed DB-side as
`jsonb_array_length(args -> 'report_ids')` — in ORM terms, `Sum(Func(KeyTransform("report_ids", "args"), function="jsonb_array_length", output_field=IntegerField()))`
grouped by `status`. Two new keys in the returned dict:

- `todo_reports` — total reports across queued subjobs
- `doing_reports` — total reports across in-flight subjobs

Both default to 0 when there are no jobs (`Sum` returns NULL → coalesce in
Python). Jobs whose `args` lack a `report_ids` key contribute NULL, which `Sum`
skips — no error. The id arrays are never transferred to Python; during a
1M-report backfill the `todo` jobs collectively hold ~1M ids, so Python-side
counting is explicitly rejected.

### Template — `radis/pgsearch/templates/admin/pgsearch/reportsearchindex/change_list.html`

- `2 queued` → `2 subjobs queued (1000 reports)`
- `4 in-flight` → `4 subjobs in-flight (2000 reports)`
- The `(N reports)` parenthetical renders only when the subjob count is nonzero;
  a zero count renders as plain `0 queued` / `0 in-flight`.
- `failed` stays a bare subjob count, unchanged.

## Testing

Extend the existing admin stats coverage (or add a test beside it): defer two
`embed_reports_task` subjobs with known `report_ids` sizes, assert
`todo_reports` equals their sum and `doing_reports` is 0; assert the rendered
changelist contains the `subjobs queued (N reports)` phrasing, and that with an
empty queue the parenthetical is absent.

## Rejected alternatives

- **Python-side summation** — simpler, but pulls every queued job's full
  `report_ids` array out of Postgres; unbounded on large backfills, which is
  exactly when the badge matters.
- **Single combined total** ("6 subjobs covering 3000 reports") — loses the
  queued/in-flight split the badge already makes.
