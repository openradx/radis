# Report withdrawal: reversible soft delete with search exclusion

**Status:** design, approved in review, not yet implemented
**Branch:** `report-withdrawal`, based on `fts-query-shape-performance` (PR #292)
**Issue:** #294 — Allow admins to withdraw (soft delete) a report and restore it later
**Depends on:** the #292 search projection. Since #292, searches never join
`reports_report`; every filter predicate is a column on
`pgsearch_reportsearchindex`, maintained by database triggers. Hiding a report
from search therefore means projecting the withdrawal state into that table.

## 1. Problem

Reports sometimes need to be taken out of circulation without being destroyed:
a report imported in error, attached to the wrong patient, or withdrawn by the
issuing department. Today the only options are leaving the report in place or
hard-deleting it, which is irreversible and loses the record that it ever
existed.

The motivating cases are ones where users must not read the report at all, so
"out of circulation" has to mean more than "absent from search results": any
existing reference — a collection entry, a note, a chat, an old browser link —
must stop exposing the report while it is withdrawn.

## 2. Decisions

The issue left six questions open. Resolved as follows:

| # | Question | Decision |
| --- | --- | --- |
| 1 | Upsert targeting a withdrawn report | The write succeeds and updates content; the report **stays withdrawn**; the response flags it. A routine re-import must not silently undo a deliberate administrative action, and rejecting would wedge ingest runs on the same record forever. |
| 2 | Naming | **Withdrawn.** Carries the right connotation for reports and avoids confusion with hard deletion. |
| 3 | Audit trail | **Fields on `Report`**: `withdrawn_at`, `withdrawn_by`, `withdrawal_reason` describe the current withdrawal; restore blanks them. Both actions write admin `LogEntry` rows as breadcrumbs. No event table — structured history across withdraw/restore cycles is not a requirement now; revisit if it becomes one. |
| 4 | Scope of exclusion | **Invisible everywhere outside the admin** (and the admin-only API). Mechanism: one fail-closed predicate in the search projection covers search, extractions and subscription refreshes; an explicit `.live()` filter covers the handful of direct-read surfaces. The default manager stays unfiltered — no soft-delete manager magic. |
| 5 | Existing references | Collection entries, notes, label results and subscription items **survive untouched** in the database. They are hidden while the report is withdrawn and reappear on restore. |
| 6 | Retention | **Deferred.** Withdrawn reports are kept indefinitely; hard delete remains available from the admin. Automatic expiry can be its own issue later. |

One correction to the issue's implementation notes: `filter()` in
`radis/pgsearch/providers.py` **does** go through `_build_filter_query` on the
current branch, so a single predicate there covers all four provider entry
points. The separate change the issue anticipated is not needed.

## 3. Data model (`reports` app)

New fields on `Report`:

- `withdrawn_at = DateTimeField(null=True, blank=True)` — `NULL` means live;
  a non-NULL value *is* the withdrawn state. No separate boolean to drift.
- `withdrawn_by = ForeignKey(AUTH_USER_MODEL, null=True, blank=True,
  on_delete=SET_NULL, related_name="+")`
- `withdrawal_reason = TextField(blank=True, default="")`
- `is_withdrawn` property (`withdrawn_at is not None`).

Query surface: a `ReportQuerySet` with `.live()`
(`withdrawn_at__isnull=True`), installed as
`objects = ReportQuerySet.as_manager()`. `Report.objects` behaves exactly as
before; `.live()` becomes available on it and on related managers
(`collection.reports.live()`).

Migrations:

- `reports/0014`: the three `AddField`s. All nullable or defaulted, so the
  migration is metadata-only and instant regardless of corpus size (same rule
  pgsearch 0003 followed).
- `reports/0015`: a partial index on `withdrawn_at WHERE withdrawn_at IS NOT
  NULL`, created with `AddIndexConcurrently` (`atomic = False`). The dedicated
  admin page filters on exactly this predicate; the index only ever contains
  withdrawn rows, so it costs live-report writes nothing and stays tiny.

## 4. Search projection (`pgsearch` app)

New projection column on `ReportSearchIndex`:

- `withdrawn = BooleanField(default=False)` — a boolean, not a timestamp
  mirror. The scan predicate only needs live/not-live, and a constant-default
  `NOT NULL` column is metadata-only in PostgreSQL.

Migrations:

- `pgsearch/0007`: the `AddField`. **No backfill migration**: at the moment
  the column lands no withdrawn report can exist yet (the feature that sets
  `withdrawn_at` ships in the same release), so `false` is already correct
  for every existing row. Rolling deploys are safe for the same reason old
  code was safe with 0003: code that predates the columns simply ignores
  them.
- `pgsearch/0008`: `CREATE OR REPLACE FUNCTION pgsearch_sync_report_fields()`
  extended with `withdrawn = (r.withdrawn_at IS NOT NULL)`. The trigger
  declaration is untouched — `pgsearch_report_fields_upd` already fires on
  every `reports_report` UPDATE, with no column list. Depends on
  `pgsearch/0007` (the target column) and `reports/0014` (the source column
  the function body reads). The reverse operation restores the 0004 function
  body.

The synced SQL copies documented in `radis/pgsearch/utils/projection.py` gain
the column: `PROJECTION_UPDATE_SQL` (which also makes the create-signal path
set it correctly for free) and `DRIFT_SQL` in `check_search_projection`. The
frozen 0005 backfill stays as-is — it predates the column — and the
"keep in sync" module docstring is updated to name 0008 as the live function.

No index on the projection column: the FTS candidate scan is a full parallel
scan by design, and `withdrawn = false` rides along at effectively zero cost.

## 5. Enforcement

### 5.1 Search provider (fail-closed core)

`_build_filter_query` in `radis/pgsearch/providers.py` gains
`Q(withdrawn=False)`. That one predicate covers all four entry points —
`search()`, `count()`, `retrieve()` and `filter()` — which means interactive
search, the extraction max-reports guard and its report collection, and
subscription refreshes all exclude withdrawn reports with no per-caller work.

### 5.2 Direct-read surfaces

Six call sites, each a one-line `.live()` (or the equivalent
`report__withdrawn_at__isnull=True` across a relation):

1. `reports.views.ReportDetailView.get_queryset()` — a withdrawn report 404s;
   `ReportBodyView` inherits the queryset, and `ReportListView` (the browse
   page) filters `.live()` as well.
2. `collections.views.CollectionDetailView.get_queryset()`
   (`collection.reports.live()`). Where a visible report count is rendered,
   count `.live()` too, so number and list agree. The Excel export
   (`export_collection`) iterates the same live set.
3. `notes.views` — `NoteListView` filters across the relation, and the
   per-report views (`NoteEditView`, `NoteAvailableBadgeView`) look the
   report up `.live()` and 404 when it is withdrawn.
4. `chats` — the report lookup for opening or continuing a chat goes
   `.live()`; the chat list hides chats whose report is withdrawn.
5. Subscriptions inbox — `SubscribedItem` listings filter across the
   relation. Items stay in the database and reappear on restore.
6. Extraction results — `ExtractionInstance.text` is a verbatim copy of the
   report body, so the task detail listing, the instance detail page, the
   result list and the CSV export all filter
   `report__withdrawn_at__isnull=True`. Instance rows survive and reappear
   on restore.

### 5.3 Worker re-checks

A report can be withdrawn between job creation and task execution. Two cheap
guards close the race: `subscriptions/processors.py` filters for live reports
where it iterates `task.reports` (a withdrawn report is never emailed), and
the extractions processor skips instances whose report is withdrawn by the
time it runs. One-line filters, no new machinery.

### 5.4 REST API (admin-only)

- `GET` list/retrieve: unchanged — admins see everything. The serializer
  gains read-only `withdrawn_at` and a computed read-only `withdrawn`
  boolean, so every response shows the state.
- `PUT ?upsert=true` on a withdrawn report: proceeds normally, updates
  content, leaves the withdrawal fields untouched (they are read-only in the
  serializer, so decision #1 falls out of the serializer contract), and the
  200 response carries `"withdrawn": true`.
- Bulk upsert: same per-item semantics; the summary response gains a
  `withdrawn` list of affected `document_id`s so pipelines can notice without
  inspecting items.
- `DELETE`: unchanged; hard delete remains a separate operation.
- Withdrawal is **not** exposed through the API in v1 — it is an admin-page
  action, per the requested behaviour. An endpoint would be an additive
  change later.

What deliberately still sees withdrawn reports: the admin, the admin-only
API, projection sync, the labels and embeddings machinery, and the delete
handlers — all through the untouched default manager.

## 6. Admin

### 6.1 Withdraw

`ReportAdmin` gains a "Withdraw selected reports" action with an intermediate
confirmation page (the `delete_selected` pattern) that requires a
**reason** before proceeding. It sets `withdrawn_at = now()`,
`withdrawn_by = request.user` and `withdrawal_reason`, skips
already-withdrawn rows, and writes a `LogEntry` per report whose message
includes the reason. The main changelist gets a withdrawn list filter and a
state column; the three withdrawal fields appear read-only on the change
form.

### 6.2 The dedicated page

A proxy model `WithdrawnReport` registered with
`WithdrawnReportAdmin(ReportAdmin)` — inheriting the existing delete handlers
and inlines — whose `get_queryset()` returns only withdrawn reports (served
by the 0015 partial index). Columns: `document_id`, patient, study,
`withdrawn_at`, `withdrawn_by`, `withdrawal_reason`. It carries the
"Restore selected reports" action (blanks the three fields, writes a
`LogEntry`), has no add permission, and hard delete remains available with
the inherited index-cleanup handlers still firing.

### 6.3 Action mechanics

Both actions use `queryset.update(...)`, not per-instance `save()`. A single
UPDATE per batch fires the projection trigger (0004's declaration, function
body from 0008), which is the only thing that needs to happen: the projection column flips and search reacts immediately. No
`post_save` signals and no `reports_updated_handlers` means no pointless
re-embedding, no label-staleness churn, and no second write to the pgsearch
row.

Known cost: each withdraw or restore is one non-HOT update of the projection
row, i.e. one insert into the HNSW index (~143 ms/row measured on staging).
Irrelevant for the intended handfuls; slow but correct if someone
bulk-withdraws thousands.

## 7. Testing

- **pgsearch**: the trigger syncs `withdrawn` on a report UPDATE; all four
  provider entry points exclude a withdrawn report; `check_search_projection`
  reports no drift after a withdraw → restore round trip.
- **API**: upsert on a withdrawn report returns 200, updates content, stays
  withdrawn, and flags it in the response; the withdrawal fields are not
  writable through the serializer; the bulk-upsert summary lists withdrawn
  ids; `GET` still returns withdrawn reports for admins.
- **Views**: all five direct-read surfaces hide a withdrawn report, and it
  reappears after restore.
- **Admin**: the reason is mandatory; withdraw sets the fields and writes a
  `LogEntry`; restore clears them; the dedicated page lists only withdrawn
  reports; delete from the dedicated page fires the delete handlers.

## 8. Documentation

A short admin-guide section: how to withdraw and restore, what withdrawal
means for search, collections and existing references, and the
upsert-stays-withdrawn rule for ingest operators. The API notes mention the
new read-only serializer fields.

## 9. Out of scope

- Automatic retention/expiry of withdrawn reports (decision #6).
- A REST endpoint for withdrawing/restoring.
- An event table with structured withdraw/restore history (decision #3).
- Excluding withdrawn reports from the labels and embeddings machinery —
  they deliberately continue to see the full corpus.
