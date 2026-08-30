# Search performance: making the FTS scan single-table

**Status:** design, not yet implemented
**Branch:** `fts-query-shape-performance`
**Supersedes:** parts of `hybrid-search.md` §7 (the FTS candidate query and its filter construction)
**Design target:** 8 million reports. The largest known deployment is ~15M; past
roughly 8M the working set stops fitting comfortably in page cache on ordinary
hardware and the conclusions in §2 need re-measuring (see §8).

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
  `search_vector`**, because `ts_rank` above the join still needs it. On the 1M
  rig (§8) with 730k matches that hash table was 1.2 GB, spilling into 256
  batches and writing ~1.2 GB of temp files at the default `work_mem`.
- `SELECT DISTINCT` blocks the top-N heapsort, forcing a full external merge sort
  of every matching row before `LIMIT` can apply.

Removing both makes the query a single-table parallel sequential scan with a
26 kB top-N heapsort. Measured at the 8M design target — same table, same four
workers on both sides — `findings` goes from **8,388 ms to 882 ms, about 9.5x**.

Parallelism is a separate and additive lever worth a further ~1.4x (§4.5). An
earlier draft of this document claimed 33x by comparing the old shape at two
workers against the new shape at eight, crediting the query-shape change with a
gain that was partly parallelism and partly a smaller corpus. The controlled
comparison above is the one to trust.

## 3. Ruled out, with measurements

Recorded because each is the obvious question a reviewer will raise.

| candidate | result |
| --- | --- |
| Lower `HYBRID_FTS_MAX_RESULTS` | No effect (1M rig). `LIMIT 100` and `LIMIT 10000` both 1.05 s. Under `ts_rank` the pool size is free; it only becomes load-bearing with a pruning index. |
| Raise `work_mem` | No effect on the 1M rig: 1.41 s at 256 MB vs 1.44 s at 4 MB. The spill is a symptom, not the cause. |
| Rewrite the joins as `EXISTS` | Worse: 1.55 s vs 1.05 s (1M rig). The planner still hashes. |
| Materialised id-only CTE, joins on narrow rows, then rank | Worse: 11,460 ms for `findings`, 3,089 ms for `pneumonia` at 5M. The joins are still over 5,001,000 rows. **This was the only way to avoid duplicating access-control data without changing the domain model (see §6), and it does not work.** |
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

Indexes — two, chosen from which callers actually populate which filters:

- **`GinIndex` on `group_ids` — measured.** On the 5M rig with the corpus split
  94.9% / 5% / 0.1% across three groups, comparing the planner's choice against
  the same query with index scans forced off:

  | group holds | with index | forced scan | gain |
  | --- | --- | --- | --- |
  | 0.1% (5,001 reports) | 2–3 ms | 345 ms | ~170x |
  | 5% (250,050) | 119–141 ms | 342–357 ms | ~2.9x |
  | 94.9% (4,745,949) | 448–452 ms | 444–487 ms | none |

  It pays wherever a group holds a minority of the corpus, and costs nothing
  where it does not — at 94.9% the planner abandons it unprompted. Carrying cost
  is negligible: 6 MB, 1.2 s to build, because a handful of distinct group ids
  means a handful of posting lists.
- **btree on `report_updated_at` — reasoned, not measured.** Subscriptions call
  `filter()` with `updated_after` (`subscriptions/tasks.py`), which has no
  tsquery to compete with, so a selective range over a timestamp is the textbook
  index case; unindexed it is a full-table scan on every subscription refresh,
  once per subscription. Worth confirming during implementation with the same
  force-the-plan technique used for `group_ids` above.

Deliberately **not** indexed:

- **`modality_codes`.** With four or five modality codes a filter selects roughly
  a quarter of the corpus, never selective enough to beat a sequential scan, and
  GIN carries real write cost. Note array operators (`@>`, `&&`) evaluate fine
  per row without any index; an index only enables *index scans*.
- **`study_datetime`.** §4.3 converts the date filters to half-open ranges, which
  would make an index on this column *usable* — but the point of that change is
  removing five million per-row timezone conversions, and it stands on its own
  without an index. A date filter always co-occurs with a tsquery, so the planner
  will usually prefer the scan anyway. Left out until a measurement justifies it.
- **`patient_id`, `report_created_at`.** No caller sets `patient_id`,
  `created_after` or `created_before` — they exist on `SearchFilters` and are
  handled in `_build_filter_query`, but nothing populates them. The columns are
  still needed for the filter code; indexes are not.
- **`patient_sex`, `patient_age`, `language_code`, `study_description`.** Low
  selectivity, or `icontains`, which a btree cannot serve anyway.

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

Statement-level, not row-level: the bulk-upsert API endpoint writes membership
rows with `group_through.objects.bulk_create(...)` and
`modality_through.objects.bulk_create(...)` (`reports/api/viewsets.py:215,229`),
so one statement can carry a whole batch. Row-level triggers would fire once per
row; with `REFERENCING NEW TABLE AS ...` each statement costs one set-based
`UPDATE ... FROM` instead.

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

**Two edges the triggers deliberately do not cover.** Deleting a `Report`
cascades to both the membership rows and the projection row; the resulting
membership-delete trigger is a zero-row `UPDATE` against an already-deleted
projection row, which is harmless in either cascade order. And `Language.code` is
treated as immutable — no trigger watches `reports_language`, so renaming a code
would strand `language_code` values until the backfill is re-run.

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
| `Q(report__patient_sex=...)`, age, description, patient_id | same, unprefixed |
| `Q(report__study_datetime__date__gte=<date>)` | `Q(study_datetime__gte=<start of that local day>)` |
| `Q(report__study_datetime__date__lte=<date>)` | `Q(study_datetime__lt=<start of the following local day>)` |
| `Q(report__created_at__gte=...)` | `Q(report_created_at__gte=...)` |
| labels | unchanged — `report_id__in=<subquery>`, no join |

**The date filters become half-open ranges.** Django's `__date` lookup compiles
to `(study_datetime AT TIME ZONE 'Europe/Berlin')::date >= …` — a function over
the column, which no plain btree can serve, and which is evaluated once per row.
On the sequential scan that dominates our plans that is five million timezone
conversions and date casts per query, for a predicate that is equivalent to a
comparison against two timestamps.

Computing the day boundaries in Python instead is exactly equivalent in meaning,
removes the per-row conversion, and makes the predicate sargable. A functional
index matching Django's expression was rejected: it would restore sargability but
leave the per-row cost untouched on the seq-scan path, and it hardcodes a
timezone that breaks silently if `TIME_ZONE` changes. Storing a precomputed local
`study_date` column was rejected for baking `TIME_ZONE` into data, which a
timezone change would then require a backfill to correct.

**`.distinct()` is removed unconditionally.** It exists because the `modalities`
join duplicates a report that matches on more than one modality (the `groups`
join cannot, since `filters.group` is a single value against a `unique_together`
through table). With no joins there is nothing to
deduplicate. Worth ~30% on its own (1.44 s → 1.04 s at 1M).

**Access-control edge case.** `filters.group=None` is documented as fail-closed:
`Q(report__groups=None)` compiles to `groups__isnull=True` and matches only
reports in no group. The array equivalent is `Q(group_ids=[])` — *not* any form
of `contains`. This is the easiest thing in the change to get subtly wrong and
gets its own test.

`match_q`, `rank_expr`, `summary_expr` and `_exclude_negations` all currently
traverse `report__language__code` and become `language_code` on the row. The
vector half also sheds its `.distinct()` (6.5 ms → 1.0 ms).

The FTS candidate query then compiles to the shape below, measured on the 5M rig
at **401 ms** with `LIMIT 10000` and `max_parallel_workers_per_gather = 4` —
confirming the production pool size costs nothing on this shape, not just the old
one:

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
   metadata-only and instant even at 5M rows. Every row therefore carries an
   empty `group_ids` until step 3 fills it, and that empty value is fail-closed
   in one direction only: a group-scoped search (`group=<id>`) compiles to
   `group_ids @> ARRAY[id]` and matches nothing, but `group=None` compiles to
   the exact match `group_ids = '{}'` and matches **every row in the archive**.
   `group=None` is reachable — `extractions/views.py` passes it for a logged-in
   user with no active group — so between this step and step 3 that path is
   fail-*open*. It is contained only accidentally today (step 1 also leaves
   `language_code` NULL corpus-wide, so the FTS half returns nothing), and the
   vector half carries no language predicate, so on a deployment with embeddings
   the leak is real. **The migration must therefore be run with the web tier
   stopped**, which is what the admin guide's upgrade procedure prescribes.
2. **Create triggers** — *before* the backfill, so reports edited during a
   multi-minute backfill still land correctly. A chunk that later reprocesses the
   same row simply rewrites the same current values.
3. **Backfill**, chunked by `report_id` range (50,000 rows) with one transaction
   per chunk (hence `atomic = False`). Each chunk is a set-based `UPDATE ... FROM
   reports_report` with two lateral `array_agg` subqueries for the arrays. The
   migration lowers `autovacuum_vacuum_scale_factor` on the table for the
   duration and resets it after, so dead tuples from one chunk are reclaimed and
   reused by the next rather than accumulating across the run. The chunk size is
   a constant, not a setting: a migration is a historical record and should do
   the same thing on every machine.
4. **Create indexes last, then `VACUUM ANALYZE`.** Building GIN indexes on empty
   columns is wasted work — but the stronger reason is that while the new columns
   are unindexed the backfill can use HOT updates, whose old row versions are
   reclaimed by page pruning without index cleanup. That is the main defence
   against the bloat below, so this ordering must not be changed. The `ANALYZE`
   is required, not hygiene: ten new columns and two new indexes carry no
   statistics, and this design depends on the planner choosing a parallel
   sequential scan with a top-N heapsort.

**Deployment consequences.** `docker-compose.prod.yml` runs `manage.py migrate`
in the `init` service and `web` waits on it, so the backfill blocks the deploy
rather than serving half-projected rows — the right failure mode for
access-control data. Deploy time therefore grows with corpus size: **measured at
9 min 20 s for 8M reports**, in 50,000-row chunks. That extrapolates to roughly
18 minutes at 15M. Adding the ten columns is genuinely free (1.3 ms, confirming
the metadata-only claim); the nine minutes is the row rewrite.

Every service that waits on `init` must therefore tolerate a wait that long:
they wait with `wait-for-it -t ${WAIT_INIT_TIMEOUT:-3600}` in both compose
files, because the previous fixed `-t 300` timed out mid-backfill and left the
containers exited after a technically successful migration.

Roughly ten minutes of announced downtime for a one-time migration has been
accepted for this deployment, which is what keeps the blocking approach below.
A site that cannot take that window needs the online alternative instead.

The backfill rewrites every row: the tsvectors average 1220 bytes and nothing is
TOASTed (`toast_heap` is 0 bytes), so the whole row is rewritten, not just the
new columns. Measured at 8M, the heap goes **10 GB → 21 GB** with 7,988,672 dead
tuples, and a plain `VACUUM` clears every dead tuple while leaving the file at
**21 GB** for ~11 GB of live data.

Chunking and creating indexes last (steps 3 and 4) did **not** prevent this — the
rows grow by ~120 bytes, so most updated tuples cannot fit back into their
original page and HOT does not apply. The doubling should be treated as expected,
not as something the migration avoids. The consequence is a sequential scan
reading roughly twice what it needs to, and the 882 ms in §2 already includes that
penalty; on a compacted table the same query would land nearer 450–650 ms. It
resolves on its own as the archive grows into the free space.

**Do not run `VACUUM FULL` from the migration, and do not recommend it blindly.**
It rebuilds every index on the table, including `pgsearch_embedding_hnsw`; on a
deployment with embeddings populated that is an HNSW rebuild over millions of
vectors — plausibly hours, under an ACCESS EXCLUSIVE lock — and it needs peak
disk for old plus new (~19 GB at 5M). Sites that want the space returned should
run it deliberately in a maintenance window knowing that cost, or use `pg_repack`
if they have it. This design requires neither. Most sites will not need it: an
archive that keeps growing refills the freed space on its own.

An online alternative — resumable backfill command plus a feature flag on the
query path — was considered and rejected: it doubles the query-layer code for
the duration and the flag has to be removed later anyway.

### 4.5 PostgreSQL configuration

Delivered as `-c` GUCs on the `postgres` service's `command:` in
`docker-compose.base.yml`, substituted from `.env`:

```yaml
postgres:
  command:
    - postgres
    - -c
    - max_parallel_workers_per_gather=${POSTGRES_MAX_PARALLEL_WORKERS_PER_GATHER:-4}
    - -c
    - max_parallel_workers=${POSTGRES_MAX_PARALLEL_WORKERS:-8}
    - -c
    - max_worker_processes=${POSTGRES_MAX_WORKER_PROCESSES:-8}
    - -c
    - shared_buffers=${POSTGRES_SHARED_BUFFERS:-128MB}
```

| variable | default | stock | rationale |
| --- | --- | --- | --- |
| `POSTGRES_MAX_PARALLEL_WORKERS_PER_GATHER` | `4` | 2 | measured 606 ms → 421 ms |
| `POSTGRES_MAX_PARALLEL_WORKERS` | `8` | 8 | unchanged default; exposed so larger hosts can raise all three coherently |
| `POSTGRES_MAX_WORKER_PROCESSES` | `8` | 8 | as above |
| `POSTGRES_SHARED_BUFFERS` | `128MB` | 128MB | unchanged default; exposed because the right value is a fraction of the host's RAM |

Only the first changes behaviour out of the box; the other three keep PostgreSQL's
own defaults and exist so operators can raise them coherently.

**None of them go in `example.env`.** The project already distinguishes the two
kinds of variable: things a deployer *chooses* are listed there (ports,
`EXAMPLE_REPORTS_LANGUAGE`, `REMOTE_DEBUGGING_PORT`), while tuning knobs with a
sensible default are compose-only overrides (`EMBEDDINGS_WORKER_CONCURRENCY`,
`WAIT_POSTGRES_TIMEOUT`, `RADIS_IMAGE`). These are the second kind. They are
documented in `docs/user-docs/admin-guide.md`, which is where operator-facing
guidance lives, and listed in the environment-variable section of `CLAUDE.md`.

Measured at 5M on `findings`:

| `max_parallel_workers_per_gather` | time |
| --- | --- |
| 2 (stock) | 606 ms |
| 4 | 421 ms |
| 6 | 379 ms |
| 8 | 343 ms |

- **`max_parallel_workers_per_gather` = 4** captures most of the available gain
  while leaving cores for concurrent searches. The cluster-wide caps
  (`max_parallel_workers`, `max_worker_processes`, both 8 by default) are *not* a
  throttle at this value — 8 covers two concurrent parallel searches, and a third
  degrades gracefully to fewer workers rather than queueing. They are exposed so
  that an operator raising `per_gather` on a larger host raises all three
  together, which is the only case where they bind.
- **`shared_buffers` default is left at PostgreSQL's own 128 MB.** Raising it is
  standard advice for a multi-GB database, but the 343 ms figure came from a warm
  *OS* page cache, so no measured gain can be attributed to it — and a raised
  default would break small installs, where 512 MB of shared memory on a 1 GB
  container fails to start or thrashes. Exposed as an override with ~25%-of-RAM
  guidance in the admin guide; not changed by default, for the same reason
  `work_mem` is not.
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

**Bulk correctness:** a `bulk_create` of many rows into the `groups` through
model — the shape the bulk-upsert endpoint uses — updates the projection for
*every* affected report. Statement-level triggers have a classic failure mode
where only one row of the transition table is processed; this pins it.

**No duplicates without `.distinct()`:** a report in several matching groups, or
with several matching modalities, appears exactly once.

**Plan-shape assertion instead of timing.** Wall-clock assertions are flaky in
CI; structure is not. Run `EXPLAIN` on the generated FTS query and assert the
plan contains no join against `reports_report_groups`, and assert the compiled
SQL contains no `DISTINCT` keyword. This deterministically catches someone
reintroducing a `report__` traversal and silently restoring the slow path.

Assert on the *candidate queryset the provider runs* — filter, tsquery match,
rank annotation, ordering and bound — and not on one rebuilt from
`_build_filter_query` alone. The `.distinct()` calls this change removes never
lived in the filter builder, so a test that rebuilds only that half cannot fail
however the production query changes. Keeping the candidate query in one named
function (`_fts_candidate_queryset`) is what makes the real thing reachable from
a test.


Do **not** assert on the absence of a `Unique` node: PostgreSQL implements
`SELECT DISTINCT` as either `Sort`+`Unique` or `HashAggregate` depending on
cardinality estimates, and measurement at 3, 1,000 and 100,000 rows — with the
same GIN array-containment pattern this design uses — produced `HashAggregate`
in every non-trivial case. An operator-name assertion therefore passes with the
regression present at exactly the scale it exists to protect. The `DISTINCT`
keyword is emitted unconditionally by Django whenever `.distinct()` is called,
so a SQL-text check is scale-independent.

**`patient_age` mirrors `Report.patient_age` exactly.** Verified that this is
the trap it guards: a BEFORE trigger on a stored generated column reads `NULL`
where the committed value was 42, so converting the triggers to BEFORE would
silently null this column out.

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
  handles 8M matches in ~880 ms. Recommendation only; closing it is its author's
  call.
- **`HYBRID_FTS_MAX_RESULTS`** stays at 10,000. Measured free under `ts_rank` on
  both shapes: `LIMIT 100` and `LIMIT 10000` were indistinguishable on the old
  joined query, and on the 5M rig `LIMIT 25` (421 ms) and `LIMIT 10000` (401 ms)
  are indistinguishable on the new single-table one. It would only become
  load-bearing with a pruning index.
- **`Report.groups` → `ArrayField`.** Would remove the redundancy altogether and
  speed up `Report.objects.filter(groups=...)` elsewhere, at the cost of FK
  integrity on an access-control field. A domain change, deliberately not bundled
  into a search-performance change.
- **Result-page N+1s.** Measured at ~134 SQL queries per 25-result page
  (`document.full_report` twice per row via `search/site.py`, `can_view_report`,
  the collections count, the notes lookup). Once the scan is ~880 ms this is
  proportionally significant, and it is the recommended next piece of work — but
  it is a different subsystem.
- **#282 (`hnsw.ef_search`) and #284 (fused result cache).** Independent,
  unaffected, still worth landing. Note `ef_search` defaults to 40 while
  `HYBRID_VECTOR_TOP_K` is 100, so the vector half currently contributes at most
  40 candidates.

## 7. Success criteria

- At the 8M design target with `max_parallel_workers_per_gather` = 4, the FTS
  candidate query drops from ~8,400 ms to under 1,000 ms immediately after the
  backfill, bloat included (measured: 8,388 ms → 882 ms).
- The FTS candidate query plan contains no join to `reports_report_groups` and
  no `Unique` node.
- `check_search_projection` reports zero drift after the backfill and after a
  round of report and group mutations.
- Existing search, extraction and subscription tests pass unchanged.

## 8. Measurement rig

The BM25 figures in §6 additionally required a custom PostgreSQL image
(`pgvector/pgvector:pg17` plus `pg_textsearch` v1.4.0), and the vector-half
figures come from a third throwaway corpus of 200,000 rows with 1024-dimension
embeddings and an HNSW index. Everything else comes from two corpora built from
`samples/reports_en.json` (1000 real-shaped report bodies, cycled with a unique
md5 suffix per report):

- **8M rig (the design target):** the 5M rig grown to 8,001,000 reports, 10 GB
  `pgsearch_reportsearchindex` heap before the backfill and 21 GB after. All §2
  and §7 figures come from here.
- **5M rig:** the dev stack database, 5,001,000 reports, ~15 GB, 6.3 GB
  `pgsearch_reportsearchindex` heap, average tsvector 1220 bytes.
- **1M rig:** a standalone container, 1,000,000 reports, 730,000 matching
  `pneumonia`, average tsvector 1607 bytes.

The benchmark host is a 31 GB Linux VM; the production target is a 128 GB M4 Max,
so an 11 GB live working set stays cached on both and these figures are a floor
rather than a ceiling. All rigs on stock PostgreSQL 17 settings (`shared_buffers` 128 MB, `work_mem` 4 MB,
`max_parallel_workers_per_gather` 2) on a 16-core host, unless a measurement
states otherwise.

**Caveats that bound these numbers.** The corpora are synthetic: 1000 distinct
body templates means lexeme distribution and `ts_rank` score diversity are less
varied than real reports (a 200k-row sample produced only 7 distinct `ts_rank`
values). Measurements are single-user with a warm OS page cache — the 404 ms
figure read 6.2 GB of buffers in ~320 ms, which is page cache, not disk. On a
production host with less RAM relative to corpus size, this path becomes
I/O-bound and degrades. That, rather than CPU, is the real scaling ceiling.
