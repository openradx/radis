# Search performance: making the FTS scan single-table

**Status:** design, not yet implemented
**Branch:** `fts-query-shape-performance`
**Supersedes:** parts of `hybrid-search.md` §7 (the FTS candidate query and its filter construction)

## 1. Problem

Hybrid search takes seconds for any query term that matches a large share of the
corpus. Measured on a synthetic 5,001,000-report corpus (§8):

| query | reports matched | page load |
| --- | --- | --- |
| `findings` | 5,001,000 | 6.5 s |
| `effusion` | 2,360,472 | 4.1 s |
| `pneumonia` | 520,104 | 3.8 s |
| `adenoma` | 5,001 | 0.17 s |

A later run measured `pneumonia` at 1.9 s after an `ANALYZE` improved the
planner's estimates; the table above is one coherent run, and the conclusion is
unchanged either way.

Cost tracks the number of *matching* reports, not corpus size. Paging is flat —
page 1 and page 400 both cost ~6.5 s — because every page re-ranks the whole
match set from scratch.

## 2. Diagnosis

`ORDER BY ts_rank(...)` has no top-k shortcut. A GIN posting list is ordered by
document id and carries no impact information, and `ts_rank` needs the whole
tsvector from the heap, so every match must be scored before `LIMIT` applies.
That part is inherent to PostgreSQL FTS and is not what this design changes.

What *is* fixable is the constant factor, and it is dominated by the joins.
`_build_filter_query` builds every predicate as a `report__` traversal, so the
FTS candidate query joins `reports_report`, `reports_report_groups` and
`reports_language`, then applies `SELECT DISTINCT`. Two consequences:

- The planner builds a hash table over the matched rows **carrying
  `search_vector`**, because `ts_rank` above the join still needs it. On the 1M rig (§8),
  730k matches, that hash table was 1.2 GB and spilled into 256 batches,
  writing ~1.2 GB of temp files at the default `work_mem`.
- `SELECT DISTINCT` blocks the top-N heapsort, forcing a full external merge sort
  of every matching row before `LIMIT` can apply.

Removing both makes the query a single-table parallel sequential scan with a
26 kB top-N heapsort. On the same 5M corpus, `findings` goes from **13,440 ms to
~600 ms** at stock parallelism, and to **~420 ms** with
`max_parallel_workers_per_gather` raised to 4 (§4.5). The plan verified in §4.3
ran in 404 ms at 8 workers.

## 3. Ruled out, with measurements

Recorded because each is the obvious question a reviewer will raise.

| candidate | result |
| --- | --- |
| Lower `HYBRID_FTS_MAX_RESULTS` | No effect (1M rig). `LIMIT 100` and `LIMIT 10000` both 1.05 s. Under `ts_rank` the pool size is free; it only becomes load-bearing with a pruning index. |
| Raise `work_mem` | No effect (1M rig) (1.41 s at 256 MB vs 1.44 s at 4 MB). The spill is a symptom, not the cause. |
| Rewrite the joins as `EXISTS` | Worse: 1.55 s vs 1.05 s (1M rig). The planner still hashes. |
| Materialised id-only CTE, joins on narrow rows, then rank | Worse: 11,460 ms for `findings`, 3,089 ms for `pneumonia` at 5M. The joins are still over 5,001,000 rows. **This was the only way to avoid duplicating access-control data, and it does not work.** |
| `strip()`ped second tsvector for ranking | Rejected (1M rig). 1.4x faster but collapses `ts_rank` to **1 distinct value** across 200,000 matching rows — `strip()` removes the positions that encode term frequency, so the ordering becomes a no-op. |
| Restricting recall ("rank only the most recent N", "refuse broad queries") | Withdrawn. No production search engine restricts recall; they restrict pagination depth and count precision. Not needed here anyway. |

## 4. Design

### 4.1 The projection

`ReportSearchIndex` is already a derived projection — `search_vector` and
`embedding` are both derived from `report.body`. It is currently half-built: it
carries what search *ranks* on but not what search *filters* on, which is why
every query has to join. This design completes it.

Governing rule: **denormalise what the scan touches, join for what the page
renders.** The scan visits millions of rows; hydration visits 25.

```python
# search projection - mirrors of the Report fields the scan filters on
group_ids         = ArrayField(IntegerField(), default=list)   # Report.groups
modality_codes    = ArrayField(CharField(16), default=list)    # Report.modalities
language_code     = CharField(max_length=10, null=True)
patient_sex       = CharField(max_length=1, null=True)
patient_age       = IntegerField(null=True)
patient_id        = CharField(max_length=64, null=True)
study_datetime    = DateTimeField(null=True)
study_description = CharField(max_length=64, blank=True, null=True)
report_created_at = DateTimeField(null=True)
report_updated_at = DateTimeField(null=True)
```

The mirrored scalars are nullable so every `AddField` stays metadata-only (§4.4);
tightening them to NOT NULL afterwards would need a validating table scan for no
benefit, and `check_search_projection` guards against drift instead.

`report_created_at` / `report_updated_at` are named for their source; an
unqualified `created_at` on this table would be ambiguous about whose timestamp
it is.

Indexes: `GinIndex` on `group_ids` and `modality_codes` (the containment and
overlap operators are unusable without them), btree on `study_datetime`. Nothing
else — the remaining predicates ride the sequential scan, exactly as they do
today, since `reports_report` carries no index on `patient_id`, `patient_sex` or
`study_datetime` either.

`body`, `pacs_name` and `document_id` stay on `Report`.

**Labels stay a subquery.** `LabelResult` churns constantly under relabeling
jobs, making `label_names` the highest-maintenance column for the least benefit.

**Cost:** ~120 bytes per row, about 600 MB at 5M reports — a ~9.5% increase on
the 6.3 GB the scan reads, in exchange for eliminating the joins.

### 4.2 Consistency

Three **statement-level** triggers using transition tables:

| trigger on | fires | maintains |
| --- | --- | --- |
| `reports_report` | AFTER UPDATE | `language_code` + the seven scalars |
| `reports_report_groups` | AFTER INSERT, DELETE | `group_ids` |
| `reports_report_modalities` | AFTER INSERT, DELETE | `modality_codes` |

Statement-level, not row-level: `bulk_upsert_report_search_indexes` writes in
5000-row chunks, and row-level triggers would fire 5000 times per statement.
With `REFERENCING NEW TABLE AS ...` each statement costs one set-based
`UPDATE ... FROM`.

The `reports_report` trigger guards its write with `IS DISTINCT FROM` across the
mirrored columns so unrelated `Report.save()` calls do not rewrite projection
rows needlessly.

The two paths that *create* `ReportSearchIndex` rows — the `post_save` receiver
in `pgsearch/signals.py` and `bulk_upsert_report_search_indexes` — populate the
projection at creation. Triggers only maintain it afterwards, so a trigger
firing before a row exists is a harmless zero-row `UPDATE`.

**Why triggers rather than `m2m_changed`.** `group_ids` governs who may read a
patient's report. An application signal fires only for Django ORM operations,
missing `add_custom_report.py`, admin actions, management commands and raw SQL.
A trigger holds inside the same transaction as the write, for every writer.
Precedent exists in the repo: `Report.patient_age` is a
`GeneratedField(db_persist=True)` computed by a hand-written SQL function
installed via `RunSQL` in `reports/migrations/0011`.

**Triggers must be AFTER, not BEFORE.** PostgreSQL does not expose stored
generated column values to BEFORE triggers, so a BEFORE trigger would mirror
`patient_age` as NULL.

**Reconciliation.** A `check_search_projection` management command verifies the
projection against its sources with one set-based query per column and reports
drift. Cheap insurance on access-control data, and something operators can run
after a restore or bulk import.

### 4.3 Query layer

`_build_filter_query` becomes a single-table predicate builder:

| today | after |
| --- | --- |
| `Q(report__groups=filters.group)` | `Q(group_ids__contains=[filters.group])` |
| `Q(report__language__code=...)` | `Q(language_code=...)` |
| `Q(report__modalities__code__in=...)` | `Q(modality_codes__overlap=...)` |
| `Q(report__patient_sex=...)`, age, dates, description, patient_id | same, unprefixed |
| `Q(report__created_at__gte=...)` | `Q(report_created_at__gte=...)` |
| labels | unchanged — `report_id__in=<subquery>`, no join |

**`.distinct()` is removed unconditionally.** It exists only because the `groups`
and `modalities` joins can duplicate rows. With no joins there is nothing to
deduplicate. Worth ~30% on its own (1.44 s → 1.04 s at 1M).

**Access-control edge case.** `filters.group=None` is documented as fail-closed:
`Q(report__groups=None)` compiles to `groups__isnull=True` and matches only
reports in no group. The array equivalent is `Q(group_ids=[])` — *not* any form
of `contains`. This is the easiest thing in the change to get subtly wrong and
gets its own test.

`match_q`, `rank_expr`, `summary_expr` and `_exclude_negations` all currently
traverse `report__language__code` and become `language_code` on the row. The
vector half also sheds its `.distinct()` (6.5 ms → 1.0 ms).

The FTS candidate query then compiles to the shape measured at 404 ms:

```sql
SELECT report_id,
       CASE WHEN language_code IN ('en')
            THEN ts_rank(search_vector, to_tsquery('english', ...)) END AS rank
FROM pgsearch_reportsearchindex
WHERE group_ids @> ARRAY[1]
  AND language_code IN ('en')
  AND search_vector @@ to_tsquery('english', ...)
ORDER BY rank DESC, report_id
LIMIT 10000;
```

**Consumers.** `search()`, `count()` and `retrieve()` inherit the fix with no
logic change; all three route through `_fuse_hybrid`. `filter()` (subscriptions)
keeps one join for `report__document_id`; it is bounded by `updated_after`, so
mirroring `document_id` would be scope for no measured gain. Hydration is
untouched — `page_rows` still `select_related("report")` for `ts_headline` over
`report__body`, which is 25 rows.

### 4.4 Migration and backfill

`pgsearch/migrations/0003_search_projection.py`, with `atomic = False`:

1. **Add columns.** Arrays get `default=list` (`DEFAULT '{}'`, NOT NULL); scalars
   go in nullable. All are constant-default or nullable, so every `AddField` is
   metadata-only and instant even at 5M rows. An empty `group_ids` is the
   fail-closed value.
2. **Create triggers** — *before* the backfill, so reports edited during a
   multi-minute backfill still land correctly. A chunk that later reprocesses the
   same row simply rewrites the same current values.
3. **Backfill**, chunked by `report_id` range with one transaction per chunk
   (hence `atomic = False`). Each chunk is a set-based `UPDATE ... FROM
   reports_report` with two lateral `array_agg` subqueries for the arrays.
4. **Create indexes last.** Building GIN indexes on empty columns and then
   filling them is wasted work and leaves bloat.

**Deployment consequences.** `docker-compose.prod.yml` runs `manage.py migrate`
in the `init` service and `web` waits on it, so the backfill blocks the deploy
rather than serving half-projected rows — the right failure mode for
access-control data. Deploy time therefore grows with corpus size: an estimated
2–5 minutes at 5M, extrapolated from analogous full-table writes (44 s for a
4M-row m2m insert, 83 s for a 4M-row tsvector update). The backfill rewrites
every row, leaving ~6 GB of dead tuples at 5M; autovacuum reclaims the space for
reuse but will not shrink the file, so sites with a maintenance window may want
`VACUUM FULL` afterwards.

An online alternative — resumable backfill command plus a feature flag on the
query path — was considered and rejected: it doubles the query-layer code for
the duration and the flag has to be removed later anyway.

### 4.5 PostgreSQL configuration

Delivered as `-c` GUCs on the `postgres` service's `command:`, driven by env vars
with defaults documented in `example.env` and `CLAUDE.md`.

Measured at 5M on `findings`:

| `max_parallel_workers_per_gather` | time |
| --- | --- |
| 2 (stock) | 606 ms |
| 4 | 421 ms |
| 6 | 379 ms |
| 8 | 343 ms |

- **`max_parallel_workers_per_gather` = 4**, with `max_worker_processes` and
  `max_parallel_workers` raised to match — all three are needed, since the
  cluster-wide caps default to 8 and would otherwise throttle it. 4 captures most
  of the available gain while leaving cores for concurrent searches.
- **`shared_buffers`** raised from the stock 128 MB, as general hygiene against a
  multi-GB database. Labelled honestly: the 343 ms figure came from a warm *OS*
  page cache, so no specific gain is attributed to this. Guidance is ~25% of RAM.
- **`work_mem` deliberately not raised.** It mattered only because `.distinct()`
  forced an external merge sort; without it the sort is a 26 kB top-N heapsort.
  Raising it is per-connection-per-node memory risk for zero measured benefit.

**GIN index:** `ALTER INDEX pgsearch_re_search__b0f715_gin SET (fastupdate = off)`
plus a one-time `gin_clean_pending_list()`. A storage-parameter change, so no
rebuild. Trade: index inserts get slower, read latency gets predictable. RADIS
writes in batches asynchronously through Procrastinate and reads interactively,
so that is the right side of the trade — but it is a trade.

## 5. Testing

**Filter equivalence via a reference oracle.** Keep the current joined
`_build_filter_query` in the test module as `_build_filter_query_legacy` and
assert old and new produce identical report-id sets across a matrix of filter
combinations. Highest-value test in the change: it catches
`modality_codes__overlap` vs `code__in` semantics, date boundaries and the
`group=None` case in one mechanism. Delete the oracle once the change settles.

**Access control:**
- `group_ids = []` is the only thing `filters.group=None` matches.
- Adding a report to a group makes it findable by that group.
- Removing a report from a group makes it unfindable — the direction that is a
  leak if the trigger is wrong.
- A raw `cursor.execute` INSERT into `reports_report_groups`, bypassing the ORM,
  still updates the projection. This test is the justification for choosing
  triggers over signals and should exist to defend that decision.

**Bulk correctness:** a `groups.set()` spanning many reports in one statement
updates all of them. Statement-level triggers have a classic failure mode where
only one row is processed; this pins it.

**No duplicates without `.distinct()`:** a report in several matching groups, or
with several matching modalities, appears exactly once.

**Plan-shape assertion instead of timing.** Wall-clock assertions are flaky in
CI; structure is not. Run `EXPLAIN` on the generated FTS query and assert the
plan contains no join against `reports_report_groups` and no `Unique` node. This
deterministically catches someone reintroducing a `report__` traversal and
silently restoring the 13-second path.

**`patient_age` is mirrored non-null**, which fails if the triggers are ever
converted to BEFORE.

Existing search, extraction and subscription tests are the regression net for the
four consumers and should pass unchanged.

## 6. Non-goals

- **BM25 / pg_textsearch (#285).** Deferred and reclassified as a *relevance*
  decision, not a performance one. Measured: as written it is **33x slower** than
  the `ts_rank` path it replaces (440,954 ms vs 13,440 ms for `findings`), because
  `.distinct()` and the joins prevent the index-ordered scan and `<@>` degenerates
  to per-row scoring at ~92 µs/row. It needs this denormalisation *plus* a
  two-phase small-k formulation to pay off (370 ms for `findings`, 242 ms for
  `pneumonia`). Index build was 6m41s and 2014 MB at 5M, holding a lock on
  `reports_report`. What BM25 still offers that PostgreSQL FTS cannot: IDF, term
  frequency saturation and length normalisation.
- **#287 impact-ordered lexeme ranks.** Likely unnecessary once plain `ts_rank`
  handles 5M matches in 343 ms. Recommendation only; closing it is its author's
  call.
- **`HYBRID_FTS_MAX_RESULTS`** stays at 10,000 — measured free under `ts_rank`.
- **`Report.groups` → `ArrayField`.** Would remove the redundancy altogether and
  speed up `Report.objects.filter(groups=...)` elsewhere, at the cost of FK
  integrity on an access-control field. A domain change, deliberately not bundled
  into a search-performance change.
- **Result-page N+1s.** Measured at ~134 SQL queries per 25-result page
  (`document.full_report` twice per row via `search/site.py`, `can_view_report`,
  the collections count, the notes lookup). Once the scan is 343 ms this is
  proportionally significant, and it is the recommended next piece of work — but
  it is a different subsystem.
- **#282 (`hnsw.ef_search`) and #284 (fused result cache).** Independent,
  unaffected, still worth landing. Note `ef_search` defaults to 40 while
  `HYBRID_VECTOR_TOP_K` is 100, so the vector half currently contributes at most
  40 candidates.

## 7. Success criteria

- `findings` (5,001,000 matches) drops from 13,440 ms to under 500 ms at the
  SQL level on the reference corpus, with `max_parallel_workers_per_gather` = 4.
- The FTS candidate query plan contains no join to `reports_report_groups` and
  no `Unique` node.
- `check_search_projection` reports zero drift after the backfill and after a
  round of report and group mutations.
- Existing search, extraction and subscription tests pass unchanged.

## 8. Measurement rig

All figures above come from two throwaway corpora built from
`samples/reports_en.json` (1000 real-shaped report bodies, cycled with a unique
md5 suffix per report):

- **5M rig:** the dev stack database, 5,001,000 reports, ~15 GB, 6.3 GB
  `pgsearch_reportsearchindex` heap, average tsvector 1220 bytes.
- **1M rig:** a standalone container, 1,000,000 reports, 730,000 matching
  `pneumonia`, average tsvector 1607 bytes.

Both on stock PostgreSQL 17 settings (`shared_buffers` 128 MB, `work_mem` 4 MB,
`max_parallel_workers_per_gather` 2) on a 16-core host, unless a measurement
states otherwise.

**Caveats that bound these numbers.** The corpora are synthetic: 1000 distinct
body templates means lexeme distribution and `ts_rank` score diversity are less
varied than real reports (a 200k-row sample produced only 7 distinct `ts_rank`
values). Measurements are single-user with a warm OS page cache — the 404 ms
figure read 6.2 GB of buffers in ~320 ms, which is page cache, not disk. On a
production host with less RAM relative to corpus size, this path becomes
I/O-bound and degrades. That, rather than CPU, is the real scaling ceiling.
