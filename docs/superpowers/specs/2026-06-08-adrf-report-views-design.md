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

### 1. Use `adrf.viewsets.GenericViewSet` + selected mixins + `DefaultRouter`

We keep the same shape as the legacy DRF `ReportViewSet`: one class subclassing `adrf.viewsets.GenericViewSet` with the create / retrieve / update / destroy async mixins from `adrf.mixins`, and a `@action(detail=False, methods=["post"], url_path="bulk-upsert")` for the bulk endpoint. URLs are wired through `rest_framework.routers.DefaultRouter`. Reasons:

- **Minimum structural diff vs. legacy.** The old class is `mixins.CreateModelMixin / DestroyModelMixin / RetrieveModelMixin / UpdateModelMixin + GenericViewSet`. The new one is the `adrf.mixins` equivalents + `adrf.viewsets.GenericViewSet`. A reviewer can read the diff as "convert sync mixins to async mixins" without re-learning a different architecture.
- **Router-generated URLs match the legacy contract for free.** `DefaultRouter` produces the same paths (`/api/reports/`, `/api/reports/{document_id}/`, `/api/reports/bulk-upsert/`) and the same route names (`report-list`, `report-detail`, `report-bulk-upsert`) the legacy code emitted, with no manual `path()`/`re_path()` work. `lookup_value_regex` defaults to `[^/.]+`, which is exactly the document-id constraint we need.
- **Browsable API root at `/api/reports/` is preserved.** `DefaultRouter` automatically adds an HTML index view there, matching legacy behavior. No regression for anyone navigating with a browser.
- **One async dispatch decision per class.** ADRF's `view_is_async` flips the entire viewset to the async dispatch path as soon as any method on it is a coroutine. Once we define `acreate`/`aretrieve`/`aupdate`/`adestroy` + the `async def bulk_upsert` action, every entry point is async. There's no per-URL flip-flopping between sync and async.

**Trade-off accepted:** `adrf.mixins` define both sync `create`/`retrieve`/`update`/`destroy` (inherited from DRF) *and* their async `a*` siblings. Our overrides target the `a*` versions; the sync versions remain on the class but are not dispatched (because `view_is_async` is True). The risk is that a future contributor sees the sync `create()` method on the inheritance chain and "fixes" it without realising the async version is what runs. We mitigate with an explicit module docstring and the async-shape guard tests (described under Tests).

### 2. Hybrid async strategy: native async ORM where clean, `database_sync_to_async` for serializer/transaction blocks

DRF serializers (`is_valid`, `save`, `data`) are entirely synchronous; ADRF does not change that. Our `ReportSerializer.create`/`update` also use `transaction.atomic()`, which has no native async context manager. So serializer + transactional blocks must be wrapped regardless of how the rest of the view is written.

We use `channels.db.database_sync_to_async` (rather than `asgiref.sync.sync_to_async`) for any wrapper that touches the database. It's a thin wrapper around `sync_to_async` that additionally closes stale DB connections after the call — the same choice ADIT makes in `adit/dicom_web/views.py`. We only fall back to plain `sync_to_async` for wrappers around code that has no DB interaction.

For simple, single-call ORM operations that don't cross a serializer or transaction (`get_object_or_404`-style lookups, `report.adelete()`, m2m `aset`), we use the native async ORM methods (`Report.objects.aget(...)`, `await report.adelete()`, etc.). This keeps the diff small and avoids unnecessary thread-pool hops on the read path without complicating the write path.

Usage map (per viewset method):

| Method | Native async ORM | `database_sync_to_async`-wrapped block |
| --- | --- | --- |
| `aretrieve` (GET /reports/{id}/) | `await Report.objects.select_related("language").aget(...)` | `self.get_serializer(report).data`; each `fetcher.fetch(report)` (gathered via `asyncio.gather`) |
| `aupdate` (PUT /reports/{id}/) | `await Report.objects.aget(...)` (upsert existence check) | `self.get_serializer(...).is_valid` + `serializer.save` + `transaction.on_commit` hookup (one block) |
| `adestroy` (DELETE /reports/{id}/) | `await Report.objects.aget(...)` | `with transaction.atomic(): report.delete()` + `transaction.on_commit` for `reports_deleted_handlers` (one block) |
| `acreate` (POST /reports/) | — | `self.get_serializer(...).is_valid` + `serializer.save` + `transaction.on_commit` hookup (one block) |
| `bulk_upsert` (POST /reports/bulk-upsert/) — `@action` | — | per-payload `is_valid` loop + `bulk_upsert_reports(...)` (one block) |

### 3. Why we are not subclassing `adrf.serializers.ModelSerializer`

Examined and rejected. `adrf.ModelSerializer.acreate` calls `raise_errors_on_nested_writes(...)`, which errors out on our nested writable `language` / `metadata` / `modalities` fields. We would have to override `acreate`/`aupdate` ourselves, and to preserve atomicity we would still wrap the body in `@sync_to_async` around a `transaction.atomic()` block. The result is the current sync `create`/`update` verbatim, wrapped in a coroutine — no cleanliness win, just a wrapper layer. `is_valid()` is also still sync in ADRF.

### 4. API contract is byte-for-byte identical

- URLs stay `/api/reports/`, `/api/reports/{document_id}/`, `/api/reports/bulk-upsert/` (generated by `DefaultRouter` from the viewset, same as the legacy code).
- URL `name=`s stay `report-list`, `report-detail`, `report-bulk-upsert` so any `reverse()` callers keep working.
- Response shapes, status codes, query-param parsing all preserved.
- PATCH returns 405. The viewset sets `http_method_names = ["get", "post", "put", "delete", "head", "options"]`, which blocks PATCH at the dispatcher level — equivalent to (and slightly clearer than) the legacy `partial_update` override that raised `MethodNotAllowed`.
- `lookup_value_regex` defaults to `[^/.]+`, which is exactly what the legacy router emitted — no explicit regex needed and `document_id` values containing `.` still 404.

## Module shape

### `radis/reports/api/urls.py` (rewritten)

```python
from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .views import ReportViewSet

router = DefaultRouter()
router.register("", ReportViewSet, basename="report")

urlpatterns = [
    path("", include(router.urls)),
]
```

### `radis/reports/api/views.py` (renamed from `viewsets.py`)

One class, `ReportViewSet`, subclassing `adrf.viewsets.GenericViewSet` plus the create / retrieve / update / destroy async mixins from `adrf.mixins`. `permission_classes = [IsAdminUser]`. Authentication classes inherit from the global `REST_FRAMEWORK` config.

Skeleton:

```python
from adrf import mixins as amixins
from adrf.viewsets import GenericViewSet

class ReportViewSet(
    amixins.CreateModelMixin,
    amixins.RetrieveModelMixin,
    amixins.UpdateModelMixin,
    amixins.DestroyModelMixin,
    GenericViewSet,
):
    queryset = Report.objects.all()
    serializer_class = ReportSerializer
    lookup_field = "document_id"
    # `lookup_value_regex` default is [^/.]+ — same as the legacy router emitted.
    permission_classes = [IsAdminUser]
    # Blocks PATCH at the dispatcher level (405). We never define
    # apartial_update/partial_update for the same effect.
    http_method_names = ["get", "post", "put", "delete", "head", "options"]

    async def acreate(self, request, *args, **kwargs):
        data = request.data

        @database_sync_to_async
        def _create():
            serializer = self.get_serializer(data=data)
            serializer.is_valid(raise_exception=True)
            report = serializer.save()
            transaction.on_commit(
                lambda: [h.handle([report]) for h in reports_created_handlers]
            )
            return serializer.data

        return Response(await _create(), status=status.HTTP_201_CREATED)

    async def aretrieve(self, request, *args, **kwargs):
        try:
            report = await Report.objects.select_related("language").aget(
                document_id=kwargs[self.lookup_field]
            )
        except Report.DoesNotExist:
            raise Http404

        data = await database_sync_to_async(
            lambda: self.get_serializer(report).data
        )()

        if request.GET.get("full", "").lower() in ("true", "1", "yes"):
            async def _fetch(f):
                return f.source, await database_sync_to_async(f.fetch)(report)
            results = await asyncio.gather(
                *(_fetch(f) for f in document_fetchers.values())
            )
            data["documents"] = {s: d for s, d in results if d is not None}

        return Response(data)

    # aupdate / adestroy follow the same pattern.

    @action(detail=False, methods=["post"], url_path="bulk-upsert")
    async def bulk_upsert(self, request):
        # per-payload validation loop + bulk_upsert_reports() inside
        # a single database_sync_to_async block.
        ...
```

`aupdate` preserves the upsert behaviour: `Report.objects.aget(...)` (sets `report=None` on `DoesNotExist`), 404 if `?upsert` is absent, otherwise `await database_sync_to_async(self.check_permissions)(clone_request(request, "POST"))` so a non-staff caller still sees 403. The save block returns `(data, 201 if report is None else 200)`.

`adestroy` does the delete and the `on_commit` registration in one `database_sync_to_async` block wrapped in `transaction.atomic()`, so the callback is bound to the same connection as the delete (raised by Gemini review and validated against pytest-django + `django_capture_on_commit_callbacks`).

`bulk_upsert` runs the per-payload `serializer.is_valid()` loop and the call to `bulk_upsert_reports(...)` inside one `database_sync_to_async` helper — identical to today's logic, just structured to live in an async action method.

## Invariants preserved

1. **Atomicity** — no `transaction.atomic()` block ever straddles a sync/async boundary.
2. **`transaction.on_commit` semantics** — created/updated/deleted handlers fire after commit, exactly as today; the bulk index enqueue still triggers via `enqueue_bulk_index_reports` (or the sync path under `settings.PGSEARCH_SYNC_INDEXING`).
3. **Validation behavior** — `serializer.is_valid(raise_exception=True)` still raises DRF `ValidationError`; ADRF's exception handler converts it to a 400 with the same body shape.
4. **Permission behavior** — `IsAdminUser` enforced on every endpoint. PUT-upsert against an unknown id still triggers the `clone_request("POST")` permission check via `get_object_or_none` (re-implemented inside `ReportViewSet.aupdate`).

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

Async-shape guard: one test imports `ReportViewSet` and asserts `inspect.iscoroutinefunction(ReportViewSet.<m>)` for each of `acreate`, `aretrieve`, `aupdate`, `adestroy`, and `bulk_upsert`. This guards against a future contributor inadvertently overriding the sync `create`/`retrieve`/`update`/`destroy` siblings inherited from the sync mixins — the dispatcher would silently switch to the sync path and break the inline-embedding follow-up.

## Risks and mitigations

| Risk | Mitigation |
| --- | --- |
| In-repo callers (e.g. `radis-client/`, other apps) `reverse()` route names that the old `DefaultRouter` produced. | Keep `name=` values identical (`report-list`, `report-detail`, `report-bulk-upsert`). Grep `radis-client/` and the rest of `radis/` for `reverse(` and `redirect(` referencing the old names before merge. |
| `transaction.on_commit` outside an atomic block runs immediately. | Same behavior as today's `perform_destroy`. Test asserts the deleted-handler runs after the delete returns. |
| `serializer.data` access lazy-loads related fields on the thread pool. | Already happens on the request thread today; not a regression. Re-use `select_related("language")` where present. |
| Sync mixin sibling methods (`create`, `retrieve`, `update`, `destroy`) remain on the class because the `adrf.mixins` inherit from the sync DRF mixins. A contributor could accidentally override the sync one. | Async-shape guard tests pin every entry point to `iscoroutinefunction` — a sync override flips the guard red. |
| Procrastinate worker tests (`radis/pgsearch/tests/test_process_embedding_*.py`) might appear affected. | They are not — `enqueue_bulk_index_reports` / `process_embedding_*` are unchanged. Confirm `uv run cli test` green before opening the PR. |

## Rollout

- Worktree already created: `.claude/worktrees/feat+adrf-views`, branch `feat/adrf-views` based on `origin/main` (commit `3e6f7540`).
- Single PR scoped to `radis/reports/api/` + `radis/reports/tests/test_report_api.py`. No migrations, no settings changes, no env vars.
- Verification before opening the PR:
  - `uv run cli lint`
  - `uv run cli test`
  - Manual smoke: `uv run cli compose-up -- --watch`, then `curl` each endpoint with a token and confirm responses match the contract.
- PR description must state explicitly: (a) no API contract change, (b) inline embedding is **not** added in this PR — that's the follow-up.
