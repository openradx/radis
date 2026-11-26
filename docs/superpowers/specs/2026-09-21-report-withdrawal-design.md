# Report withdrawal: reversible soft delete with search exclusion

**Status:** implemented in PR #308
**Issue:** #294 — Allow admins to withdraw (soft delete) a report and restore it later
**Builds on:** the #292 search projection. Search candidate queries never join
`reports_report`; every filter predicate is a column on
`pgsearch_reportsearchindex`, maintained by database triggers. Hiding a report
from search therefore means projecting the withdrawal state into that table.

## 1. Problem

Reports sometimes need to be taken out of circulation without being destroyed:
a report imported in error, attached to the wrong patient, or withdrawn by the
issuing department. Before this feature the only options were leaving the
report in place or hard-deleting it, which is irreversible and loses the record
that it ever existed.

The motivating cases are ones where users must not read the report at all, so
"out of circulation" has to mean more than "absent from search results": any
existing reference — a collection entry, a note, a chat, an old browser link —
must stop exposing the report while it is withdrawn.

## 2. Decisions

| # | Question | Decision |
| --- | --- | --- |
| 1 | Upsert targeting a withdrawn report | The write succeeds and updates content; the report **stays withdrawn**; the response flags it. A routine re-import must not silently undo a deliberate administrative action, and rejecting would wedge ingest runs on the same record forever. |
| 2 | Naming | **Withdrawn.** Carries the right connotation for reports and avoids confusion with hard deletion. |
| 3 | Audit trail | **Fields on `Report`** describe the current withdrawal: `withdrawn_at`, `withdrawn_by`, `withdrawal_reason`. Restore blanks them. No history of earlier withdrawals is kept: no admin log entries, no event table (decided in review). |
| 4 | Scope of exclusion | **Invisible everywhere outside the admin** and the staff-only API. One fail-closed predicate in the search projection covers search, extractions and subscription refreshes; explicit filters cover the direct-read surfaces. The default manager stays unfiltered. |
| 5 | Existing references | Collection entries, notes, chats, label results, inbox items and extraction results **survive untouched** in the database. They are hidden while the report is withdrawn and come back on restore. |
| 6 | Retention | **Deferred.** Withdrawn reports are kept indefinitely; hard delete through the API is unaffected. |
| 7 | Admin page | **One Reports admin** with a "By withdrawn" filter and both actions; no page of its own for withdrawn reports (decided in review). |

## 3. Data model (`reports` app)

Fields on `Report`:

- `withdrawn_at = DateTimeField(null=True, blank=True)` — `NULL` means live;
  a non-NULL value *is* the withdrawn state. No separate boolean to drift.
- `withdrawn_by = ForeignKey(AUTH_USER_MODEL, null=True, blank=True,
  on_delete=SET_NULL, related_name="+", db_index=False)` — no index, because
  nearly every row is NULL and nothing filters on it; deleting a user scans
  `reports_report` instead, which is rare.
- `withdrawal_reason = TextField(blank=True, default="", db_default="")` — the
  database default keeps rows inserted without the column working.
- `is_withdrawn` property (`withdrawn_at is not None`).

Query surface: `ReportQuerySet.live()` (`withdrawn_at__isnull=True`) through an
explicit `ReportManager`, so `Report.objects.live()` and related managers
(`collection.reports.live()`) have it. `Report.objects` itself stays
unfiltered.

Migrations:

- `reports/0014`: the three `AddField`s. Nullable or with a constant default,
  so the migration is metadata-only regardless of corpus size.
- `reports/0015`: a partial index on `withdrawn_at WHERE withdrawn_at IS NOT
  NULL`, created with `AddIndexConcurrently` (`atomic = False`). The admin's
  Withdrawn filter uses exactly this predicate; the index only ever contains
  withdrawn rows, so live-report writes do not pay for it.

## 4. Search projection (`pgsearch` app)

New projection column on `ReportSearchIndex`:

- `withdrawn = BooleanField(default=False, db_default=False)` — a boolean, not
  a timestamp mirror: the scan predicate only needs live or not. The constant
  default keeps the `AddField` metadata-only, and keeping it in the database
  lets code from before the column insert index rows.

Migrations:

- `pgsearch/0006`: the `AddField`. **No backfill**: when the column lands no
  withdrawn report can exist yet (the feature that sets `withdrawn_at` ships in
  the same release), so `false` is already correct for every row.
- `pgsearch/0007`: `CREATE OR REPLACE FUNCTION pgsearch_sync_report_fields()`
  extended with `withdrawn = (c.withdrawn_at IS NOT NULL)`. The trigger
  declaration from 0003 is untouched — `pgsearch_report_fields_upd` already
  fires on every `reports_report` UPDATE, with no column list. Depends on
  `pgsearch/0006` (the target column) and `reports/0014` (the source column).
  The reverse operation restores the 0003 function body.

The other copies of the scalar columns gain the column too:
`PROJECTION_UPDATE_SQL` in `radis/pgsearch/utils/projection.py` (which also
makes the create path set it) and `DRIFT_SQL` in `check_search_projection`. The
0004 backfill predates the column and stays as it is.

No index on the projection column: the candidate scan is a full parallel scan
by design, and `withdrawn = false` rides along at effectively zero cost.

## 5. Enforcement

### 5.1 Search provider

`_build_filter_query` in `radis/pgsearch/providers.py` gains
`Q(withdrawn=False)`. Every provider entry point builds its WHERE clause there:
`search()` in both modes (the keyword ranking and the semantic scan),
`count()`, `retrieve()` and `filter()`. So interactive search, the extraction
count and cohort, and subscription refreshes all exclude withdrawn reports.

Rankings are cached (`SEARCH_RANKING_CACHE_TIMEOUT_SECONDS`), and a page is
built from the cached ids. `_hydration_queryset` therefore checks
`withdrawn = false` again, next to its group check, so a report withdrawn after
the ranking was cached leaves the page at once; the cached count includes it
until the entry expires. A restored report joins a cached ranking only when the
entry expires.

### 5.2 Direct-read surfaces

Each filters with `Report.objects.live()` or, from a related model,
`report__withdrawn_at__isnull=True`:

1. Reports: the report list, detail and body views.
2. Collections: the list's report count, the detail page, the Excel export,
   adding a report by id and the count badge.
3. Notes: the list, the detail, the edit view and the badge.
4. Chats: the list and detail (chats without a report stay), starting a chat
   about a report (page and POST) and continuing one.
5. Subscriptions: the list's counts, the inbox, the inbox CSV and the job's
   finished mail.
6. Extractions: the task detail, the instance detail (staff included), the
   result list and the CSV export.

### 5.3 Worker re-checks

A report can be withdrawn between job creation and task execution.
`subscriptions/processors.py` processes only live reports, so a withdrawn one
never reaches the LLM or an inbox. The extractions processor skips instances
whose report is withdrawn and ends the task with a warning that counts them, so
the job does not claim to have extracted from them.

### 5.4 REST API (staff only)

- `GET` retrieve: unchanged — staff see every report. The serializer gains
  read-only `withdrawn_at`, `withdrawn_by`, `withdrawal_reason` and a computed
  `withdrawn` boolean.
- `PUT` and `PUT ?upsert=true` on a withdrawn report update its content and
  leave the withdrawal fields untouched: they are read-only, and the
  serializer saves only the fields an update sets, so an admin's withdraw or
  restore made while the request runs is not overwritten.
- Bulk upsert: same per-item semantics; the summary gains an always-present
  `withdrawn` list of the affected `document_id`s.
- `DELETE`: unchanged.
- Withdrawing and restoring are not exposed through the API.

What deliberately still sees withdrawn reports: the admin, the API, projection
sync, the labeling and embedding machinery, and deletion.

## 6. Admin

The Reports admin gets:

- a "By withdrawn" filter (Yes/No) and a Withdrawn column;
- search by exact document ID, which uses the unique index (the default
  `icontains` would scan the whole archive);
- the three withdrawal fields read-only on the change form;
- two actions, both requiring the change permission on reports.

**Withdraw selected reports** shows a confirmation page that requires a
reason, skips already-withdrawn reports, and sets `withdrawn_at = now()`,
`withdrawn_by = request.user` and the reason. The page posts every report back
as a hidden input and Django rejects requests with more than
`DATA_UPLOAD_MAX_NUMBER_FIELDS` (1,000) fields, so a selection above 500 is
refused with a message before the page is shown.

**Restore selected reports** blanks the three fields of the withdrawn reports
in the selection and moves `updated_at`. Subscriptions select reports by
`updated_at`, and a refresh that ran while the report was withdrawn moved
`last_refreshed` past it, so without the move a restored report would never
reach those subscriptions. The cost is that the next labeling scan re-labels
the report. An extraction job that skipped the report is not re-run.

Both actions use `queryset.update(...)`, not per-instance `save()`. One UPDATE
per selection fires the projection trigger, which is all the search needs; no
`post_save` and no `reports_updated_handlers` means no re-embedding and no
tsvector rewrite. Messages count the rows the UPDATE changed.

Known cost: each withdraw or restore updates the projection row, which with
embeddings meant one insert into the HNSW index (~143 ms per row measured on
staging). Irrelevant for the intended handfuls.

## 7. Testing

- **pgsearch**: the trigger syncs `withdrawn` on withdraw and restore, also for
  several reports in one UPDATE; the migration's reverse SQL restores the
  previous function; `check_search_projection` reports no drift after a round
  trip and detects drift in the column; `search()` in both modes, `count()`,
  `retrieve()` and `filter()` exclude a withdrawn report, also when the page
  comes from a ranking cached before the withdrawal.
- **API**: upsert on a withdrawn report updates content and stays withdrawn;
  the withdrawal fields cannot be set through an upsert; a stale instance
  saved after a withdrawal keeps the withdrawal; the bulk summary lists
  withdrawn ids.
- **Views**: every surface in 5.2 hides a withdrawn report.
- **Workers**: both processors skip withdrawn reports, the extraction task ends
  with a warning, the finished mail leaves the report out, and a report
  withdrawn during a refresh reaches the subscription after restore.
- **Admin**: the reason is mandatory; withdraw sets the fields; restore clears
  them and moves `updated_at`; the filter, the search, the selection limit,
  the read-only change form and the permission check; the confirmation page
  posts back what the action needs.

## 8. Documentation

The admin guide's "Withdrawing Reports" section (how to withdraw, find and
restore, what a restore triggers, the API fields and upsert rule), a paragraph
in the user guide, and the search-cache freshness notes in the admin guide and
settings.

## 9. Out of scope

- Automatic retention or expiry of withdrawn reports (decision 6).
- A REST endpoint for withdrawing or restoring.
- A history of withdrawals (decision 3).
- Excluding withdrawn reports from the labeling and embedding machinery.
