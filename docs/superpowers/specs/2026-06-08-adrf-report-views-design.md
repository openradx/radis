# ADRF Report Views — Design

**Date:** 2026-06-08
**Branch:** `feat/adrf-views` (worktree off `origin/main`)
**Status:** Approved, ready for plan

## Motivation

We want to embed each uploaded report **inline, during the upload request**, by calling the async embedding client from inside the view handler. Today, embedding only happens out-of-band via the periodic `embedding_launcher` cron tick that scans for `ReportSearchVector.embedding IS NULL` rows. Moving the embedding into the request path means the report's vector is populated by the time the API returns, so downstream search is correct on the very first query — no eventual-consistency window between upload and indexing.

The embedding client is I/O-bound (an HTTP call to the embedding service). For it to be inline without serializing every request behind one thread, the view handler has to `await` the client coroutine and yield to the event loop while the embedding call is in flight. DRF's `ViewSet`/`GenericViewSet` are synchronous and cannot do that; ADRF (`adrf` — already installed and listed in `INSTALLED_APPS`) provides async-compatible `APIView` equivalents that can.

This PR is the structural prerequisite: replace the existing DRF `ReportViewSet` with explicit ADRF `APIView` classes, following the same pattern ADIT already uses in `adit/dicom_web/views.py`. No client-visible contract change; no inline embedding wiring yet — that lands in a follow-up that adds `await embedding_client.embed_document(report.body)` to the create/update paths and writes the result to `ReportSearchVector.embedding` before responding.

## Scope

**In scope**

- Drop `radis.reports.api.viewsets.ReportViewSet` and the `DefaultRouter` registration.
- Add three `adrf.views.APIView` subclasses covering all five existing endpoints:
  - `ReportListAPIView` — `POST /api/reports/` (create)
  - `ReportDetailAPIView` — `GET`/`PUT`/`DELETE` on `/api/reports/{document_id}/`
  - `ReportBulkUpsertAPIView` — `POST /api/reports/bulk-upsert/`
- Rewrite `radis/reports/api/urls.py` to wire explicit `path()` entries (no router).
- Keep `_bulk_upsert_reports` (currently in `viewsets.py`) reused as-is; it stays a pure sync function.
- Preserve every existing wire-level behavior: URLs, response shapes, status codes, permission checks (including the `clone_request("POST")` check on PUT-upsert that hits an unknown `document_id`), the `?upsert=` / `?full=` / `?replace=` query parameters, and the 405 for PATCH.
- New test file `radis/reports/tests/test_report_api.py` exercising each endpoint end-to-end via Django's `Client`.
- Preserve existing `radis/reports/tests/test_bulk_upsert.py` (no payload changes needed). Add one assertion confirming the bulk-upsert route still resolves.

**Out of scope (called out to prevent scope creep)**

- Wiring the inline async embedding call into the create/update paths. That is the follow-up PR.
- Touching `ReportSerializer` — it stays sync.
- Converting any other API surface (`radis.search`, `radis.chats`, `radis.extractions`, etc.).
- Migrations, settings, or env-var changes.

## Decisions and rationale

### 1. Drop the viewset entirely; follow ADIT's pattern

We use three explicit `adrf.views.APIView` subclasses wired via `path()` entries rather than `adrf.viewsets`. Reasons:

- Matches ADIT's `adit/dicom_web/views.py` pattern, which the team already maintains.
- A `DefaultRouter` would still be needed for the viewset variant; explicit paths are simpler and let `bulk-upsert/` and `<document_id>/` be ordered unambiguously.
- All five endpoints become async with one consistent class hierarchy. No mixed sync/async viewset shape.

### 2. Hybrid async strategy: native async ORM where clean, `database_sync_to_async` for serializer/transaction blocks

DRF serializers (`is_valid`, `save`, `data`) are entirely synchronous; ADRF does not change that. Our `ReportSerializer.create`/`update` also use `transaction.atomic()`, which has no native async context manager. So serializer + transactional blocks must be wrapped regardless of how the rest of the view is written.

We use `channels.db.database_sync_to_async` (rather than `asgiref.sync.sync_to_async`) for any wrapper that touches the database. It's a thin wrapper around `sync_to_async` that additionally closes stale DB connections after the call — the same choice ADIT makes in `adit/dicom_web/views.py`. We only fall back to plain `sync_to_async` for wrappers around code that has no DB interaction.

For simple, single-call ORM operations that don't cross a serializer or transaction (`get_object_or_404`-style lookups, `report.adelete()`, m2m `aset`), we use the native async ORM methods (`Report.objects.aget(...)`, `await report.adelete()`, etc.). This keeps the diff small and avoids unnecessary thread-pool hops on the read path without complicating the write path.

Usage map:

| Endpoint | Native async ORM | `database_sync_to_async`-wrapped block |
| --- | --- | --- |
| `GET /reports/{id}/` | `await Report.objects.select_related("language").aget(...)` | `serializer.data`; each `fetcher.fetch(report)` |
| `PUT /reports/{id}/` | `await Report.objects.aget(...)` (upsert existence check) | `serializer.is_valid` + `serializer.save` + `transaction.on_commit` hookup (one block) |
| `DELETE /reports/{id}/` | `await Report.objects.aget(...)`, `await report.adelete()` | `transaction.on_commit` for `reports_deleted_handlers` |
| `POST /reports/` | — | `serializer.is_valid` + `serializer.save` + `transaction.on_commit` hookup (one block) |
| `POST /reports/bulk-upsert/` | — | per-payload `is_valid` loop + `_bulk_upsert_reports(...)` (one block) |

### 3. Why we are not subclassing `adrf.serializers.ModelSerializer`

Examined and rejected. `adrf.ModelSerializer.acreate` calls `raise_errors_on_nested_writes(...)`, which errors out on our nested writable `language` / `metadata` / `modalities` fields. We would have to override `acreate`/`aupdate` ourselves, and to preserve atomicity we would still wrap the body in `@sync_to_async` around a `transaction.atomic()` block. The result is the current sync `create`/`update` verbatim, wrapped in a coroutine — no cleanliness win, just a wrapper layer. `is_valid()` is also still sync in ADRF.

### 4. API contract is byte-for-byte identical

- URLs stay `/api/reports/`, `/api/reports/{document_id}/`, `/api/reports/bulk-upsert/`.
- URL `name=`s match what `DefaultRouter` produced today (`report-list`, `report-detail`, `report-bulk-upsert`) so any `reverse()` callers keep working. Grep before merge; adjust if a name diverges.
- Response shapes, status codes, query-param parsing all preserved.
- PATCH still returns 405; this is now achieved by simply not defining `async def patch`, instead of the current explicit `raise MethodNotAllowed`.

## Module shape

### `radis/reports/api/urls.py` (rewritten)

```python
from django.urls import path

from .views import (
    ReportBulkUpsertAPIView,
    ReportDetailAPIView,
    ReportListAPIView,
)

urlpatterns = [
    path("", ReportListAPIView.as_view(), name="report-list"),
    path("bulk-upsert/", ReportBulkUpsertAPIView.as_view(), name="report-bulk-upsert"),
    path("<str:document_id>/", ReportDetailAPIView.as_view(), name="report-detail"),
]
```

`bulk-upsert/` is listed before `<str:document_id>/` to avoid the path converter swallowing the literal segment.

### `radis/reports/api/views.py` (renamed from `viewsets.py`)

Three `adrf.views.APIView` subclasses, each with `permission_classes = [IsAdminUser]`. Authentication classes inherit from the global `REST_FRAMEWORK` config.

Representative handler shapes:

```python
class ReportDetailAPIView(AsyncApiView):
    permission_classes = [IsAdminUser]

    async def get(self, request, document_id):
        try:
            report = await Report.objects.select_related("language").aget(
                document_id=document_id
            )
        except Report.DoesNotExist:
            raise Http404

        data = await database_sync_to_async(
            lambda: ReportSerializer(report, context={"request": request}).data
        )()

        if request.GET.get("full", "").lower() in ("true", "1", "yes"):
            documents: dict[str, Any] = {}
            for fetcher in document_fetchers.values():
                doc = await database_sync_to_async(fetcher.fetch)(report)
                if doc is not None:
                    documents[fetcher.source] = doc
            data["documents"] = documents

        return Response(data)
```

```python
class ReportListAPIView(AsyncApiView):
    permission_classes = [IsAdminUser]

    async def post(self, request):
        @database_sync_to_async
        def _do_create():
            serializer = ReportSerializer(
                data=request.data, context={"request": request}
            )
            serializer.is_valid(raise_exception=True)
            report = serializer.save()
            transaction.on_commit(
                lambda: [h.handle([report]) for h in reports_created_handlers]
            )
            return serializer.data
        data = await _do_create()
        return Response(data, status=status.HTTP_201_CREATED)
```

`ReportDetailAPIView.put` preserves the existing upsert special case (today's `get_object_or_none` + `clone_request("POST")` permission check + 201 on create). `ReportDetailAPIView.delete` reuses `Report.objects.aget(...)` + `report.adelete()` and schedules the deleted-handler via `transaction.on_commit` inside one tiny `database_sync_to_async` block.

`ReportBulkUpsertAPIView.post` does the per-payload `serializer.is_valid()` loop and the call to `_bulk_upsert_reports(...)` inside one `database_sync_to_async` helper — identical to today's logic, just structured to live in an async view.

## Invariants preserved

1. **Atomicity** — no `transaction.atomic()` block ever straddles a sync/async boundary.
2. **`transaction.on_commit` semantics** — created/updated/deleted handlers fire after commit, exactly as today; the bulk index enqueue still triggers via `enqueue_bulk_index_reports` (or the sync path under `settings.PGSEARCH_SYNC_INDEXING`).
3. **Validation behavior** — `serializer.is_valid(raise_exception=True)` still raises DRF `ValidationError`; ADRF's exception handler converts it to a 400 with the same body shape.
4. **Permission behavior** — `IsAdminUser` enforced on every endpoint. PUT-upsert against an unknown id still triggers the `clone_request("POST")` permission check via `get_object_or_none` (re-implemented inside `ReportDetailAPIView.put`).

## Tests

Existing:

- `radis/reports/tests/test_bulk_upsert.py` keeps passing without payload changes. Add one assertion that the bulk-upsert route still resolves (regression guard for the router removal).

New: `radis/reports/tests/test_report_api.py` with end-to-end coverage via Django's `Client`:

- `POST /api/reports/` → 201; full `ReportSerializer` roundtrip; `reports_created_handlers` fires.
- `GET /api/reports/{document_id}/` → 200; basic shape.
- `GET /api/reports/{document_id}/?full=true` → 200; includes `documents` from a stub `document_fetcher` registered for the test.
- `PUT /api/reports/{document_id}/` happy-path → 200; fields updated.
- `PUT /api/reports/{document_id}/?upsert=true` against a missing id → 201; record created.
- `PUT /api/reports/{document_id}/?upsert=true` as a non-staff user → 403 (proves the `clone_request("POST")` permission check still fires).
- `PATCH /api/reports/{document_id}/` → 405.
- `DELETE /api/reports/{document_id}/` → 204; `reports_deleted_handlers` fires.
- `POST /api/reports/bulk-upsert/` with `replace=false` → 400; with a mixed create+update payload → 200 plus the expected `{created, updated, invalid}` counts.

Async-shape guard: one test asserts `asyncio.iscoroutinefunction(ReportListAPIView.post)` (and the same for the other handlers) so a future refactor cannot silently regress to sync.

## Risks and mitigations

| Risk | Mitigation |
| --- | --- |
| In-repo callers (e.g. `radis-client/`, other apps) `reverse()` route names that the old `DefaultRouter` produced. | Keep `name=` values identical (`report-list`, `report-detail`, `report-bulk-upsert`). Grep `radis-client/` and the rest of `radis/` for `reverse(` and `redirect(` referencing the old names before merge. |
| `transaction.on_commit` outside an atomic block runs immediately. | Same behavior as today's `perform_destroy`. Test asserts the deleted-handler runs after the delete returns. |
| `serializer.data` access lazy-loads related fields on the thread pool. | Already happens on the request thread today; not a regression. Re-use `select_related("language")` where present. |
| Browsable API root at `/api/reports/` disappears with the router. | Acceptable; this is an admin-only token-auth endpoint, not user-facing. Note in PR description. |
| Procrastinate worker tests (`radis/pgsearch/tests/test_process_embedding_*.py`) might appear affected. | They are not — `enqueue_bulk_index_reports` / `process_embedding_*` are unchanged. Confirm `uv run cli test` green before opening the PR. |

## Rollout

- Worktree already created: `.claude/worktrees/feat+adrf-views`, branch `feat/adrf-views` based on `origin/main` (commit `3e6f7540`).
- Single PR scoped to `radis/reports/api/` + `radis/reports/tests/test_report_api.py`. No migrations, no settings changes, no env vars.
- Verification before opening the PR:
  - `uv run cli lint`
  - `uv run cli test`
  - Manual smoke: `uv run cli compose-up -- --watch`, then `curl` each endpoint with a token and confirm responses match the contract.
- PR description must state explicitly: (a) no API contract change, (b) inline embedding is **not** added in this PR — that's the follow-up.
