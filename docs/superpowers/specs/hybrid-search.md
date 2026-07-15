# Hybrid Search Design (FTS + Dense Vector via Qwen3-Embedding-4B)

**Status:** Implemented on `feat/hybrid-search` — living document, last synced to code 2026-07-14. Exception: §6.8 (report-centric badge + backfill runs) designed 2026-07-15, implementation pending.
**Author:** RADIS team (Samuel Kwong)
**Date:** 2026-05-28
**History:** Single consolidated spec for hybrid search. The per-increment design docs it absorbed (initial 2026-05-15 design; embedding client OpenAI-SDK migration; pipeline logging; rate-limit gate; rate-limit generalization research; backfill cancel + throughput knobs; shared 429 backoff; admin badge subjob report counts) were removed 2026-07-14 and remain in git history. The consolidated implementation history lives in `docs/superpowers/plans/hybrid-search.md`.

---

## 1. Overview

RADIS today provides PostgreSQL full-text search (FTS) over radiology reports via the `radis.pgsearch` provider: each `Report` gets a 1:1 `ReportSearchIndex` row holding a `tsvector`, kept in sync via `post_save` signal and a bulk re-index task. Queries are ranked by `ts_rank` and snippeted via `ts_headline`.

This spec extends that infrastructure with a dense-vector retrieval side, fused with FTS via Reciprocal Rank Fusion (RRF), to deliver **hybrid search**. Embeddings are produced by a Qwen3-Embedding-4B inference endpoint and stored in the same `ReportSearchIndex` table.

The public `SearchProvider` API (`radis.search.site`) is unchanged. Callers — `SearchView`, `ExtractionJob`, `SubscriptionJob`, the REST API — see no signature differences. Only the body of `radis.pgsearch.providers.search()` and `retrieve()` changes.

## 2. Goals & non-goals

### Goals

- Combine the existing FTS recall with semantic recall so queries like "no pneumothorax" surface reports that describe the absence without containing the exact word (modulo the dense-retrieval polarity limitation in §11).
- Keep the existing `SearchProvider` contract intact.
- Index embeddings asynchronously without blocking report ingest.
- Keep embedding load isolated from chat/extraction/subscription LLM tasks.
- Degrade gracefully when the embedding service is unavailable (search continues as FTS-only).
- Talk to any OpenAI-compatible embeddings endpoint (`EMBEDDING_PROVIDER_URL` ending in `/v1`) so Ollama's `/v1` compatibility layer works in dev and a vLLM/gateway-served Qwen3 endpoint works in prod with the same code path.

### Non-goals

- No new search-provider plugin slot. The single `pgsearch` provider continues to be the only one registered.
- No per-query UI toggle for semantic vs. lexical. Hybrid is the new default.
- No Vespa, Elasticsearch, or OpenSearch adapter.
- No solution for negation/polarity (§11 documents this as known future work).
- No automated re-embedding when `EMBEDDING_DIM` changes. That is a manual operator procedure: drop column, re-migrate, re-PUT affected reports (see §4.5).
- No on-disk vector quantization. Float32 storage from day one; revisit if RAM pressure appears.

## 3. Architecture

```
┌──────────────────────────────────────────────────────────────────────┐
│  SearchView, REST API, ExtractionJob, SubscriptionJob                │
└──────────────┬───────────────────────────────────────────────────────┘
               │ Search(query, filters, offset, limit)
               ▼
┌──────────────────────────────────────────────────────────────────────┐
│  radis.pgsearch.providers.search()       (hybrid, replaces FTS-only) │
│                                                                      │
│  1. embed_query() ──► EmbeddingClient ──► Qwen3 endpoint             │
│     on failure: query_vec = None                                     │
│                                                                      │
│  2. Vector top-K   ────► ReportSearchIndex  (HNSW on .embedding)    │
│                          filtered by structured filters              │
│                                                                      │
│  3. FTS hits       ────► ReportSearchIndex  (GIN on .search_vector) │
│                          filtered by structured filters              │
│                                                                      │
│  4. Python-side RRF fusion of (vec_top_K ∪ fts_hits)                 │
│  5. Pagination on the fused order                                    │
│  6. ts_headline() ────► ReportSearchIndex  (page-slice only)        │
└──────────────────────────────────────────────────────────────────────┘

┌──────────────────────────────────────────────────────────────────────┐
│  Async indexing path  (handler-registry → deferred via Procrastinate)│
│                                                                      │
│  Report view  (single-create / PUT / bulk-upsert)                    │
│        │                                                             │
│        ▼  transaction.atomic() block                                 │
│  ReportSerializer / bulk_upsert_reports                              │
│    ├─ DB write (Report rows)                                         │
│    └─ transaction.on_commit:                                         │
│         dispatches reports_created_handlers / reports_updated_       │
│         handlers (radis.reports.site registry) with the touched      │
│         Report instances                                             │
│        │                                                             │
│        ▼  (one of the registered subscribers is pgsearch:)           │
│  pgsearch._index_reports(reports)                                    │
│    ├─ PGSEARCH_SYNC_INDEXING=True:                                   │
│    │     bulk_upsert_report_search_indexes(report_ids) inline,       │
│    │     then enqueue_embed_reports(report_ids)                      │
│    └─ PGSEARCH_SYNC_INDEXING=False:                                  │
│          enqueue_bulk_index_reports(report_ids); the embed subjobs   │
│          are chained at the tail of bulk_index_reports (see below)   │
│        │                                                             │
│        ▼  HTTP response returned (201 / 200) immediately             │
│                                                                      │
│  ──── elsewhere, on the default_worker process ────                  │
│                                                                      │
│  bulk_index_reports(report_ids)   (default queue)                    │
│    ├─ bulk_upsert_report_search_indexes(report_ids)                  │
│    └─ enqueue_embed_reports(report_ids)   (subjob chunking)          │
│                                                                      │
│  ──── elsewhere, on the embeddings_worker process ────               │
│                                                                      │
│  embed_reports_task(report_ids)   (embeddings queue, one per subjob) │
│    ├─ load RSIs (select_related("report"))                           │
│    ├─ _embed_chunk_with_retry: rate-limit gate → transient retries  │
│    │     → EmbeddingClient.embed_documents([body, ...])  (batched)   │
│    ├─ L2-normalize; ReportSearchIndex.objects.bulk_update           │
│    └─ on failure: raise                                              │
│         → Procrastinate retry policy (exp backoff, transient only)   │
└──────────────────────────────────────────────────────────────────────┘
```

`radis.reports` already exposes a handler registry (`reports_created_handlers` / `reports_updated_handlers` in `radis.reports.site`) whose docstring is explicit about its purpose: *"The handler can be used to index those reports in an external search database."* Pgsearch registers `_index_reports` on both. The view layer never imports anything from `pgsearch`; it only dispatches the registry.

Both ingest paths — single-create (`POST /api/reports/`, `PUT /api/reports/{id}/?upsert=true`) and bulk-upsert (`POST /api/reports/bulk-upsert/`) — flow through the same handler, which schedules a Procrastinate task on the dedicated `embeddings` queue (directly in sync FTS mode; chained at the end of `bulk_index_reports` in deferred FTS mode). The write path returns immediately after the transaction commits; the embedding service is touched only by the worker. This:

- **Decouples write-path uptime from the embedding service.** API responses succeed even when the embedding endpoint is down or slow.
- **Bounds concurrent load on the embedding service** via the worker's `--concurrency K` — explicit, configurable backpressure rather than implicit request-driven concurrency.
- **Auto-recovers from transient outages** via Procrastinate's retry policy with exponential backoff.
- **Inverts the dependency** so `radis.reports` stays unaware of search/indexing concerns; adding or swapping a search provider is a registration call, not a view edit.
- **Symmetric across single-create and bulk-upsert** — one enqueue site, one task, one worker.

**Components added inside `radis.pgsearch`:**

| File | Purpose |
|---|---|
| `utils/embedding_client.py` | `EmbeddingClient` used by both the query path and `embed_reports_task` on the worker. Sync client over the `openai` SDK against a single OpenAI-compatible endpoint (`EMBEDDING_PROVIDER_URL` ending in `/v1`); SDK retries disabled (`max_retries=0`) so the rate-limit gate and transient-retry helper own all retry policy. Also hosts the process-global `EMBEDDING_GATE`. |
| `apps.py` (modified) | `register_app()` now also registers `_index_reports` on both `reports_created_handlers` and `reports_updated_handlers`. In sync FTS mode the handler upserts inline then calls `enqueue_embed_reports`; in deferred FTS mode it enqueues `bulk_index_reports`, which chains the embed subjobs at the end of its own run. Also home of the `pgsearch.E001`/`E002` system checks (§4.6). This is the only place pgsearch wires itself into the reports app. |
| `tasks.py` (embedding entries) | `enqueue_embed_reports(report_ids)` — the single chunking point that defers one `embed_reports_task` per `EMBEDDING_SUBJOB_SIZE` chunk, at live or backfill priority. `embed_reports_task(report_ids)` on the `embeddings` queue loads RSIs by `report_id`, embeds through `_embed_chunk_with_retry` (gate + transient retries, §6.2), then `bulk_update`s. Failures propagate so `EMBEDDING_TASK_RETRY_STRATEGY` applies. |
| `admin.py` | Registers `ReportSearchIndex` with a `has_embedding` list display, an `embedding` `IsNull` filter, the embedding-pipeline badge on the changelist (report-centric with subjob detail secondary, §6.8), and two actions: `enqueue_pending_embeddings` (defers embed subjobs for selected NULL rows at backfill priority) and `clear_embeddings` (NULLs embeddings, e.g. before a same-dim model swap). A separate cancel-backfill view cancels still-queued backfill subjobs (also available as the `embed_cancel` management command). Mirrors `embed_pending` for operators who prefer the UI. Also registers a read-only `EmbeddingBackfillRun` listing (run history, §6.8). |
| `migrations/0002_hybrid_search.py` | Single squashed schema migration: renames `ReportSearchVector` → `ReportSearchIndex`, `CREATE EXTENSION vector`, adds the `embedding vector(1024)` column, the HNSW index, and a partial index on `embedding IS NULL` rows (backs the admin's pending-embedding count) |
| `models.py` (modified) | `ReportSearchVector` renamed to `ReportSearchIndex`; adds the `embedding` field, `HnswIndex`, and the `pgsearch_pending_embedding_idx` partial index. Also `EmbeddingBackfillRun` (per-backfill progress state, §6.8). No Job/Task models. |
| `signals.py` (unchanged from FTS-only) | The FTS `create_or_update_report_search_vector` receiver stays; **no embedding signal** |
| `tasks.py` (FTS bits) | FTS bulk-indexing helper `bulk_upsert_report_search_indexes` and the `bulk_index_reports` Procrastinate task. `bulk_index_reports` upserts the RSI rows and then calls `enqueue_embed_reports(...)` at the end of its run, so the embeddings worker only ever sees report ids whose RSI rows are already committed (see §6.6). |
| `providers.py` (modified) | Replaces `search()` and `retrieve()` bodies with hybrid logic |
| `tests/...` | Coverage per §10 |

**Infrastructure additions:**

| File | Change |
|---|---|
| `pyproject.toml` | Add `pgvector>=0.3` dependency |
| `radis/settings/base.py` | New env-driven + constant settings (§8) |
| `radis/settings/test.py` | Override `EMBEDDING_PROVIDER_URL=""` so any incidental construction of `EmbeddingClient` fast-fails into `EmbeddingClientError` in CI (no live embedding service). Tests that exercise embedding patch the client explicitly. |
| `example.env` | Documents the `EMBEDDING_*` env vars (provider URL with OpenAI/Ollama `/v1` examples, API key, model, dim, optional batch/subjob/timeout overrides) and `EMBEDDINGS_WORKER_CONCURRENCY` |
| `radis/reports/api/viewsets.py` | **Unchanged from main** in shape. It already dispatches `reports_created_handlers` / `reports_updated_handlers` from `on_commit`; pgsearch hooks in via that registry. Nothing in `viewsets.py` imports from `radis.pgsearch`. |

## 4. Schema and migrations

### 4.1 Dependency

Add to `pyproject.toml`:

```toml
"pgvector>=0.3",
```

### 4.2 Schema migration

Schema lives in a single file `radis/pgsearch/migrations/0002_hybrid_search.py`,
depending on `pgsearch.0001_initial` and `reports.0013_alter_report_options`,
squashed from the intermediate branch migrations so hybrid search ships as one
coherent migration rather than three states no operator will ever see in
isolation. Operations:

1. `RunSQL("CREATE EXTENSION IF NOT EXISTS vector;", reverse_sql=RunSQL.noop)`.
   Reverse is a no-op because the extension may be shared with other Postgres
   usage and dropping it would damage unrelated state. Dev rollback is handled
   by recreating the database.
2. `RenameModel` `ReportSearchVector` → `ReportSearchIndex` (the row now holds
   the FTS tsvector *and* the dense embedding; named after its role, not any
   single field), including the reverse accessor on `Report`
   (`search_vector` → `search_index`).
3. `AddField` `embedding` on `ReportSearchIndex`:
   `pgvector.django.vector.VectorField(dimensions=settings.EMBEDDING_DIM, null=True)`
   (captured as `vector(1024)`).
4. `AddIndex` HNSW on `embedding`: `m=16`, `ef_construction=64`,
   `opclasses=["vector_cosine_ops"]`, `name="pgsearch_embedding_hnsw"`.
5. `AddIndex` partial index `pgsearch_pending_embedding_idx` on rows with
   `embedding IS NULL`: the admin changelist counts pending embeddings on
   every request, and the HNSW index cannot serve an `IS NULL` predicate —
   without this that count is a sequential scan over millions of rows.

The all-deferred embedding architecture (§6) has no orchestrator tables or
system user, so this migration carries only schema. Reverse drops the indexes
and column.

### 4.4 Model update

`radis/pgsearch/models.py`:

```python
from django.conf import settings
from pgvector.django import HnswIndex, VectorField

class ReportSearchIndex(models.Model):
    report = models.OneToOneField(Report, on_delete=models.CASCADE, related_name="search_index")
    search_vector = SearchVectorField(null=True)
    embedding = VectorField(dimensions=settings.EMBEDDING_DIM, null=True)

    class Meta:
        indexes = [
            GinIndex(fields=["search_vector"]),
            HnswIndex(
                name="pgsearch_embedding_hnsw",
                fields=["embedding"],
                m=16,
                ef_construction=64,
                opclasses=["vector_cosine_ops"],
            ),
            # Backs the admin's pending-embedding count (HNSW can't serve IS NULL).
            models.Index(
                fields=["id"],
                condition=models.Q(embedding__isnull=True),
                name="pgsearch_pending_embedding_idx",
            ),
        ]
```

`embedding` is nullable: the row exists from the moment a `Report` is created (FTS path), but its embedding is filled by the `embed_reports_task` Procrastinate worker, enqueued from `transaction.on_commit` (§6). A NULL embedding is treated as "not embedded yet" at query time, and the row participates via the FTS half only.

`save()` on `ReportSearchIndex` retains its current behavior of recomputing `search_vector` from `report.body`. The embedding column is written **only** by `embed_reports_task` via `bulk_update()`, never by `save()`, to avoid triggering the FTS signal recursively and to keep the two indexing paths independent.

### 4.5 Operational note on `EMBEDDING_DIM`

pgvector columns and HNSW indexes are bound to a fixed dimension at create time, and HNSW has a 2000-dim ceiling (so `EMBEDDING_DIM ≤ 2000`; Qwen3-Embedding-4B's native 2560 is Matryoshka-truncated client-side). Changing `EMBEDDING_DIM` after deploy requires a manual operator procedure:

1. Drop the HNSW index and the `embedding` column.
2. Re-run `0002_hybrid_search` with the new `EMBEDDING_DIM`. This re-creates
   the column at the new dim plus the HNSW index.
3. Run `./manage.py embed_pending` to enqueue an `embed_reports_task` for
   every row that's now NULL. The command is idempotent and resumable; the
   embeddings worker drains the queue at its configured `--concurrency`.
   See §6.5.
4. From here on, new writes enqueue tasks against the new dim automatically.

This is documented as a deployment-time decision and intentionally not automated.

### 4.6 Startup safety check for env/migration drift

Two Django system checks guard against the failure mode where
`settings.EMBEDDING_DIM` no longer matches what the squashed
`0002_hybrid_search` migration describes. Without these the divergence would
surface later as an opaque pgvector dimension error on the first write or
query.

The migration-side dim is *not* stored in a hand-edited constant. Instead it
is derived at check time from Django's `MigrationLoader` project state —
built from the migration files on disk without a database connection — so
there is exactly one source of truth (the `dimensions=...` literal that
`makemigrations` itself generated from `settings.EMBEDDING_DIM` when
`0002_hybrid_search` was first written).

```python
# radis/pgsearch/apps.py

def _migration_embedding_dim() -> int | None:
    """Return the `dimensions` value of `ReportSearchIndex.embedding` as
    captured by the on-disk pgsearch migrations. Returns None if the field
    cannot be located (e.g., migrations are missing or out of sync)."""
    from django.db.migrations.loader import MigrationLoader

    loader = MigrationLoader(connection=None, ignore_no_migrations=True)
    state = loader.project_state()
    try:
        model = state.apps.get_model("pgsearch", "ReportSearchIndex")
        return model._meta.get_field("embedding").dimensions
    except (LookupError, AttributeError):
        return None


@register()
def check_embedding_dim_matches_migration(app_configs, **kwargs):
    migration_dim = _migration_embedding_dim()
    if migration_dim is None:
        return [Error(
            "Could not determine the embedding column dimension from the "
            "pgsearch migrations.",
            id="pgsearch.E002",
            hint="Verify that radis/pgsearch/migrations/ contains a migration "
                 "that adds `embedding` to `ReportSearchIndex`.",
        )]
    if settings.EMBEDDING_DIM != migration_dim:
        return [Error(
            f"EMBEDDING_DIM={settings.EMBEDDING_DIM} does not match the dim "
            f"baked into the pgsearch migrations (vector({migration_dim})). "
            f"Either set EMBEDDING_DIM={migration_dim}, or run "
            f"`makemigrations pgsearch` to capture the new dim and follow §4.5.",
            id="pgsearch.E001",
        )]
    return []
```

Check IDs:

| ID | When it fires |
|---|---|
| `pgsearch.E001` | `settings.EMBEDDING_DIM != migration_dim`. The familiar drift case. |
| `pgsearch.E002` | `_migration_embedding_dim()` returns `None`. Indicates the migration tree is missing the `embedding` field — either it was deleted without replacement, or the model was renamed. Surfaces what would otherwise be a silent NoneType crash. |

Alternatives considered and rejected:

| Option | Authoritative for | DB connection | Verdict |
|---|---|---|---|
| Hand-edited constant (status quo before this change) | Nothing — must be manually transcribed | No | Drift-prone |
| Parse `migrations/0002_hybrid_search.py` source | The literal in one specific file | No | Brittle; couples to filename |
| `MigrationLoader` project state | The aggregated dim across all migrations | No | Chosen |
| `information_schema.columns` on the live DB | The actually-deployed column dim | Yes | Loses the offline-check property |

`MigrationLoader.project_state()` reflects the *post-all-migrations* state, so
if a later migration drops and recreates the column at a different dim, the
check stays correct without any code change to `apps.py`.

## 5. Embedding client

### 5.1 Module layout

`radis/pgsearch/utils/embedding_client.py` exposes:

- `EMBEDDING_GATE: RateLimitGate` — process-global 429 backoff window shared by every embedding caller in the worker/web process. Deliberately separate from the LLM gate in `core.utils.llm_client`: the embedding gateway is a different provider, so a 429 from one must not pause the other.
- `class EmbeddingClientError(Exception)` — malformed responses (count/dim mismatch) and invalid configuration. Typed `openai.OpenAIError` subclasses (`RateLimitError`, `BadRequestError`, `InternalServerError`, …) are *not* wrapped in this class; callers that discriminate (the transient retry layer, the rate-limit gate) match on the SDK types directly.
- `class EmbeddingClient` — sync client over the `openai` SDK, used by both the query path (`providers.search` / `providers.retrieve`) and the `embed_reports_task` worker task (§6.2). A single client class keeps the configuration surface narrow; worker-side concurrency is provided by Procrastinate's `--concurrency K` flag spawning K sync task slots, not by intra-task asyncio.

### 5.2 Interface

```python
class EmbeddingClient:
    def __init__(self) -> None:
        # Raises EmbeddingClientError if EMBEDDING_PROVIDER_URL is unset.
        # "unused" is the documented api_key placeholder for self-hosted
        # endpoints that ignore auth (Ollama, vLLM).
        self._client = openai.OpenAI(
            base_url=settings.EMBEDDING_PROVIDER_URL,
            api_key=settings.EMBEDDING_PROVIDER_API_KEY or "unused",
            http_client=_build_http_client(),  # test seam for httpx.MockTransport
            max_retries=0,  # 429s belong to the rate-limit gate, not the SDK
            timeout=settings.EMBEDDING_REQUEST_TIMEOUT,
        )

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """One POST {base}/embeddings call (encoding_format="float"). Returns
        L2-normalized vectors of length EMBEDDING_DIM (Matryoshka-truncating
        longer vectors, §5.4). No 429/transient handling of its own — the
        callers own that (embed_query below; _embed_chunk_with_retry in
        tasks.py). HTTP errors propagate as typed SDK exceptions."""

    def embed_query(self, text: str) -> list[float]:
        """Prepend EMBEDDING_QUERY_INSTRUCTION, then embed through
        EMBEDDING_GATE with the short query budget
        (EMBEDDING_RATE_LIMIT_QUERY_MAX_WAIT_SECONDS): a user is waiting, so
        a gate closed beyond that budget raises RateLimited and the provider
        falls back to FTS-only."""
```

The client is a context manager (`close()` releases the underlying `httpx.Client`); both call sites use `with EmbeddingClient() as client:`.

### 5.3 Wire protocol

One protocol: the OpenAI embeddings API. `EMBEDDING_PROVIDER_URL` is an OpenAI-compatible base URL ending in `/v1`; the SDK posts to `{base}/embeddings` with `{"model": M, "input": [t, ...], "encoding_format": "float"}` and reads `{"data": [{"embedding": [...]}, ...]}`. The same shape covers OpenAI, Azure, vLLM, an LLM gateway, and Ollama's `/v1` compatibility layer — swapping providers is a URL change, not a code path. (`encoding_format="float"` opts out of the SDK's base64 default: no decode step, and a debuggable wire format.)

An earlier iteration had pluggable request/response backends (`openai` vs. Ollama's native `/api/embed` plus an `EMBEDDING_PROVIDER_PATH` override); that was dropped when the client moved onto the `openai` SDK, since every targeted provider speaks the `/v1` shape.

### 5.4 Behavior details

- **Query instruction:** the model card for Qwen3-Embedding recommends a task-specific instruction prefix on the query side only. `embed_query` prepends `EMBEDDING_QUERY_INSTRUCTION` (a Python constant in `base.py`); `embed_documents` does not.
- **Overlength inputs:** the client does *not* truncate input text. The model's context window is the authoritative limit; the backend rejects overlength input with an HTTP 4xx that surfaces as a typed SDK error (usually `openai.BadRequestError`). There is no special-case detection or per-report isolation (the earlier `EmbeddingPayloadTooLargeError` + chunk-bisect design was removed 2026-07-02 — see §6.2): on the worker, `BadRequestError` is in no retry set, so the subjob fails permanently; on the query path it falls back to FTS-only for that request.
- **Normalization:** every returned vector is L2-normalized client-side, unconditionally. With unit vectors, cosine distance is monotonic in dot product, which makes the HNSW `vector_cosine_ops` operator effectively a fast inner-product search. Whether the upstream server normalizes is irrelevant.
- **Dimension validation & Matryoshka truncation:** vectors shorter than `EMBEDDING_DIM` raise `EmbeddingClientError`; longer vectors are truncated to the first `EMBEDDING_DIM` components and renormalized (Qwen3-Embedding is trained to retain quality at truncated dims). Exact-length vectors are still normalized, since providers can't be assumed to return unit vectors.
- **Batching:** `embed_documents` sends a single HTTP call per invocation. The write path and `embed_pending` both go through `enqueue_embed_reports(report_ids)` (defined in `tasks.py`), which chunks the input by `EMBEDDING_SUBJOB_SIZE` and defers one `embed_reports_task` per subjob. Inside each task, `EMBEDDING_BATCH_SIZE` controls the per-HTTP-call size. See §6.3 for the three-layer batching model.
- **Errors:** malformed responses (count mismatch, short vectors) and missing configuration raise `EmbeddingClientError`; HTTP-level failures (429, 4xx, 5xx, timeouts) propagate as typed `openai` SDK exceptions so the retry layers can discriminate. The client never falls back internally — fallback policy is owned by the caller.
- **Dev recipe (Ollama):**
  ```bash
  ollama pull dengcao/Qwen3-Embedding-4B:Q5_K_M
  # in .env (Ollama's OpenAI-compat layer):
  EMBEDDING_PROVIDER_URL=http://host.docker.internal:11434/v1
  EMBEDDING_MODEL_NAME=dengcao/Qwen3-Embedding-4B:Q5_K_M
  EMBEDDING_DIM=1024   # model's native 2560 is Matryoshka-truncated client-side
  ```
  GGUF-quantized embedding models produce slightly different vectors than the bf16 reference, so dev embeddings are not interchangeable with prod embeddings. After swapping the model between dev/prod, clear the column (`ReportSearchIndex.objects.update(embedding=None)`) and run `./manage.py embed_pending`.

## 6. Async indexing (deferred via Procrastinate)

Every successful report write enqueues an async Procrastinate task that embeds the report(s) on a dedicated worker queue. The write path is decoupled from the embedding service's uptime, transient outages auto-recover via Procrastinate's retry policy, and load on the embedding service is bounded by worker concurrency rather than request concurrency.

### 6.1 The enqueue at write time

`viewsets.py` is unchanged from main — it already dispatches `reports_created_handlers` / `reports_updated_handlers` inside `transaction.on_commit`. Pgsearch subscribes to those at app startup:

```python
# radis/pgsearch/apps.py

def _index_reports(reports):
    if not reports:
        return
    report_ids = [report.pk for report in reports]
    if settings.PGSEARCH_SYNC_INDEXING:
        bulk_upsert_report_search_indexes(report_ids)
        enqueue_embed_reports(report_ids)
    else:
        # bulk_index_reports chains enqueue_embed_reports at the end of its
        # run, so the embeddings worker never sees a report id before its
        # RSI row is committed.
        enqueue_bulk_index_reports(report_ids)

# inside register_app():
register_reports_created_handler(
    ReportsCreatedHandler(name="PG Search", handle=_index_reports)
)
register_reports_updated_handler(
    ReportsUpdatedHandler(name="PG Search", handle=_index_reports)
)
```

The view contributes nothing pgsearch-specific. Whatever fires `reports_created_handlers` / `reports_updated_handlers` (the API viewsets, the Django admin's `save_model`, any future caller) automatically gets FTS + embedding for free.

When the `transaction.atomic()` block commits:

1. Report rows are durable.
2. RSI rows exist (or will exist once `bulk_index_reports` runs, in the deferred FTS mode — see §6.6).
3. A row is inserted into `procrastinate_jobs` describing the embedding work.

The HTTP response returns at that point. The view handler does **not** await embedding.

### 6.2 The task

`radis/pgsearch/tasks.py`:

```python
@app.task(queue="embeddings", retry=EMBEDDING_TASK_RETRY_STRATEGY)
def embed_reports_task(report_ids: list[int]) -> None:
    if not report_ids:
        return

    rsvs = list(
        ReportSearchIndex.objects.filter(report_id__in=report_ids)
        .select_related("report")
        .only("id", "report_id", "report__body")
    )
    if len(rsvs) < len(report_ids):
        logger.warning(...)  # ids with no RSI row are named and skipped
        if not rsvs:
            return

    batch_size = settings.EMBEDDING_BATCH_SIZE
    embedded: list[ReportSearchIndex] = []
    try:
        with EmbeddingClient() as client:
            for start in range(0, len(rsvs), batch_size):
                chunk = rsvs[start : start + batch_size]
                vectors = _embed_chunk_with_retry(
                    client, [rsv.report.body for rsv in chunk]
                )
                for rsv, vec in zip(chunk, vectors, strict=True):
                    rsv.embedding = vec
                    embedded.append(rsv)
    except EmbeddingClientError:
        logger.error(...)  # named ids; will be retried by Procrastinate
        raise

    if embedded:
        ReportSearchIndex.objects.bulk_update(embedded, fields=["embedding"])
```

**Sync, not async**: each task issues batches sequentially (one HTTP round-trip at a time, waiting for the response before launching the next), so asyncio inside a single task wouldn't add concurrency. Worker concurrency comes from Procrastinate's `--concurrency K` flag, which gives K independent task slots regardless of whether the task body is `def` or `async def`. A sync task keeps the call graph readable — direct ORM, direct `httpx.Client`, no `database_sync_to_async` shims.

**Internal batching**: chunking happens at *enqueue* time, not inside the task. `enqueue_embed_reports(report_ids)` splits any input into subjobs of `EMBEDDING_SUBJOB_SIZE` (env, default 1000) and defers one `embed_reports_task` per subjob. Inside each task, the subjob is further chunked into HTTP calls of `EMBEDDING_BATCH_SIZE` reports each (env, default 200). This decouples the *subjob size* (Procrastinate-task granularity: retry blast radius, parallel drain) from the *embedding service call size* (always bounded by `EMBEDDING_BATCH_SIZE`). The endpoint sees a steady stream of equally-sized batches rather than occasional spike requests. Enqueues also carry a priority: `EMBEDDING_LIVE_PRIORITY` (1) for write-path work, `EMBEDDING_BACKFILL_PRIORITY` (0) for `embed_pending` / the admin backfill action, so a million-row backfill cannot park itself ahead of live ingest.

**No per-report isolation for overlength inputs** (changed 2026-07-02): an earlier design detected payload-too-large responses via a typed `EmbeddingPayloadTooLargeError` and recursively bisected the failing chunk to isolate and skip the offending report. That machinery was removed together with the proactive rate limiter — the loose substring matching used to classify "too large" errors was brittle across providers. Today an overlength input surfaces as `openai.BadRequestError` from the SDK. It is in no retry set (deterministic failure), so the whole subjob fails permanently in `procrastinate_jobs` with the `report_ids` named in the job row. The affected reports' embeddings stay NULL; the operator fixes the upstream report (or raises the model's context window) and re-runs `embed_pending`, which re-enqueues only the still-NULL rows. The blast radius is one subjob (`EMBEDDING_SUBJOB_SIZE` reports), not the whole backfill.

**Three layers of failure handling for the embed call**: `_embed_chunk_with_retry` composes the shared helpers from `radis.core.utils.rate_limit` — the same stack the LLM client uses — with Procrastinate's task-level retry above:

```python
def _embed_chunk_with_retry(client: EmbeddingClient, texts: list[str]) -> list[list[float]]:
    return run_through_gate(
        EMBEDDING_GATE,
        settings.EMBEDDING_RATE_LIMIT_MAX_WAIT_SECONDS,
        lambda: with_transient_retries(
            lambda: client.embed_documents(texts),
            settings.EMBEDDING_TRANSIENT_RETRY_ATTEMPTS,
            settings.EMBEDDING_TRANSIENT_RETRY_BASE_SECONDS,
            retryable=(*TRANSIENT_ERRORS, EmbeddingClientError),
        ),
    )
```

- **`with_transient_retries` (innermost, per-call):** `EMBEDDING_TRANSIENT_RETRY_ATTEMPTS` (default 2) retries after the first call — 3 total calls — with exponential backoff `base * 2**attempt` (0.5 s, 1 s). Handles brief blips: connection errors, timeouts (`APITimeoutError` subclasses `APIConnectionError`), 5xx, and malformed responses surfaced as `EmbeddingClientError`. `RateLimitError` (429) is deliberately *not* in the retryable tuple, so it passes straight through to the gate. Each retry logs a WARNING.
- **`run_through_gate` / `EMBEDDING_GATE` (middle, 429s):** a per-process gate shared by every embedding caller in the worker/web process. On a 429 it pauses all of them together, honouring the server's `Retry-After` (or an exponential ladder), within a single `EMBEDDING_RATE_LIMIT_MAX_WAIT_SECONDS` (300 s) budget computed once per call. If the budget is exceeded, it raises `RateLimited`.
- **Procrastinate (outermost, per-task):** `RateLimited` and exhausted transient errors escape the task, and `EMBEDDING_TASK_RETRY_STRATEGY` retries the whole subjob with exponential spacing (6 s, 36 s, ~4 min, ~22 min across `EMBEDDING_TASK_MAX_ATTEMPTS = 5`). Handles extended outages where the embedding service is down for minutes-to-hours. Retry is scoped to `{RateLimited, EmbeddingClientError, *TRANSIENT_ERRORS}` so deterministic misconfiguration (bad credentials, wrong model name) fails the subjob immediately instead of burning retries. On retry the entire batch loop reruns (idempotent: `bulk_update` overwrites identical vectors with no change).
- **Why three layers and not one:** local retries absorb the common case of "the service blipped once" without the operator-visible noise of a Procrastinate retry event, and without re-doing the task bookkeeping. The gate turns provider rate limiting into coordinated waiting instead of a retry storm across concurrent callers. Procrastinate above the task covers the long-tail outage the local layers are not budgeted for.

For tests, `with_transient_retries` resolves its default sleep (`time.sleep`) at call time, so tests monkeypatch `time.sleep` in `radis.core.utils.rate_limit` and exercise retry behaviour without real waits.

### 6.3 The worker and the concurrency model

A dedicated `embeddings_worker` container is added to `docker-compose.*.yml` with an explicit concurrency flag. Dev hardcodes `--concurrency 4` (plus `--autoreload`); prod reads it from the environment:

```yaml
# docker-compose.prod.yml
embeddings_worker:
  <<: *default-app
  command: |
    bash -c "
      wait-for-it -s postgres.local:5432 -t ${WAIT_POSTGRES_TIMEOUT:-180} &&
      ./manage.py bg_worker -q embeddings --concurrency ${EMBEDDINGS_WORKER_CONCURRENCY:-2}
    "
```

Three explicit choices:

- **Dedicated queue (`embeddings`)**: isolated from `default` (extraction / subscription) and `llm`. A backfill or write burst can't starve unrelated tasks.
- **`--concurrency K`** (the concurrency knob; dev 4, prod `EMBEDDINGS_WORKER_CONCURRENCY`, default 2): up to K `embed_reports_task` slots in flight on the worker at once. Each slot processes its batches sequentially, so `--concurrency K` translates directly to "up to K embedding HTTP requests in flight to the embedding service per worker process." Total system concurrency = `worker_count × --concurrency`. Keeping K modest leaves capacity for the query path's `embed_query` to share the same embedding service. Tunable per deployment.
- **Sync task body**: the task is `def`, not `async def`. Procrastinate gives concurrency through K independent task slots regardless of sync vs async, and the embedding batch loop is sequential by design — switching to async would not add any in-task concurrency, just a `database_sync_to_async` shim layer.

**Three layers of "batching"**, easy to confuse, kept separate by design:

| Layer | Knob | What it controls |
|---|---|---|
| Per-Procrastinate-task size | `EMBEDDING_SUBJOB_SIZE` (env; default 1000) | How many report ids one `embed_reports_task` instance carries. The single chunking point for *every* enqueue — write-path handler, FTS chain tail, `embed_pending`, admin action — via `enqueue_embed_reports(report_ids)`. |
| Per-HTTP-call size | `EMBEDDING_BATCH_SIZE` (env; default 200) | How many report bodies are sent in one `embed_documents` call *inside* one task. One subjob of 1000 → 5 HTTP calls of 200. |
| Concurrent task slots per worker | `--concurrency K` (compose flag; dev 4, prod default 2) | How many `embed_reports_task` instances run in parallel on a single worker. |
| Concurrent HTTP calls across all workers | `worker_count × --concurrency K` | The system's actual load ceiling on the embedding service. |

Why subjob granularity matters: a 1M-row `embed_pending` backfill becomes ~1k subjobs of 1000, not one giant task. Multiple workers can drain in parallel; a stuck or failing subjob has bounded blast radius (retries reprocess only 1000 ids, not 1M); Procrastinate's `--concurrency K` actually means something for backfill throughput. Write-path bulk-upserts get the same treatment: chunked at the same knob before they hit the queue.

To scale up, prefer adding worker processes (crash isolation + connection-pool fan-out) over raising `--concurrency` past ~8 (the embedding service typically saturates around there anyway). Total embedding load on the service is `worker_count × --concurrency`.

### 6.4 Failure semantics

Procrastinate handles transient failures automatically; `embed_pending` (§6.5) handles extended outages.

| Failure | What happens |
|---|---|
| **Brief blip** (single 5xx / timeout / network jitter ≲ seconds) | `with_transient_retries` inside the task retries the same HTTP call (up to 3 total calls, 0.5 s / 1 s backoff). Most cases recover before the task even completes its current batch loop iteration. No Procrastinate retry event. |
| **Rate limiting (429)** | The per-process `EMBEDDING_GATE` pauses every embedding caller in the process together, waiting out the server-reported `Retry-After` (or an exponential ladder) within one 300 s budget per call. If the budget runs out, `RateLimited` escapes → Procrastinate retries the subjob later. |
| **Transient outage** (service degraded for minutes; outlasts the local retries) | Local retries exhaust → exception escapes the task → Procrastinate's task-level retry kicks in with exponential backoff. Most cases auto-recover; the embedding is written without operator action. |
| **Extended outage** (service down longer than Procrastinate's retry window) | Task ends in `failed` state in `procrastinate_jobs`. embedding stays NULL. Operator runs `./manage.py embed_pending` (or the admin action) once the service recovers to re-enqueue the affected rows. |
| **Wrong-dim vector returned by backend** | `EmbeddingClientError` raised → retries → all fail the same way → task ends `failed`. Operator inspects, fixes config (or the `pgsearch.E001` system check catches it at deploy time). |
| **Worker offline / crashed** | Tasks pile up in `procrastinate_jobs.todo`. When a worker starts, it picks them up via `SELECT ... FOR UPDATE SKIP LOCKED`. No data loss. Write path unaffected. |
| **Embedding written and report immediately deleted** | `bulk_update` updates zero rows for the deleted RSI row; rest of the batch is unaffected. Benign. |
| **`EMBEDDING_PROVIDER_URL` empty / misconfigured** | `EmbeddingClient.__init__` raises `EmbeddingClientError` at task start → retries fail → task ends `failed`. Operator fixes settings, runs `embed_pending`. |
| **`settings.EMBEDDING_DIM` ≠ migration dim** | `pgsearch.E001` system check blocks startup; this is caught at deploy time, not runtime. |

The **write path never fails because of embedding**. Reports are saved, FTS indexed sync, vector indexing best-effort with retries + recovery.

### 6.5 `embed_pending` — operator-driven recovery

The `./manage.py embed_pending` command **enqueues `embed_reports_task` subjobs** rather than running embedding work inline in the command process. This keeps the embedding service load bounded by the worker's configured concurrency rather than by however fast the operator's shell can iterate. It is a plain sync `handle()`: select `report_id`s where `embedding IS NULL` (ordered, optionally capped by `--limit`), then hand them to the shared chunking helper:

```python
subjob_count = enqueue_embed_reports(
    ids,
    subjob_size=opts["subjob_size"],  # default settings.EMBEDDING_SUBJOB_SIZE
    priority=settings.EMBEDDING_BACKFILL_PRIORITY,
)
```

`--subjob-size` overrides the Procrastinate-task granularity per run; `--limit N` stops after enqueuing N reports (useful for a canary batch). Backfill priority keeps the enqueued subjobs behind live write-path work. A running backfill can be cancelled with `cancel_backfill_embeddings()` (exposed as the admin "cancel backfill" view), which cancels every still-`todo` backfill-priority subjob and stamps `cancelled_at` on active `EmbeddingBackfillRun` rows (§6.8); continuing later means simply re-running `embed_pending`.

Each invocation (and each use of the admin `enqueue_pending_embeddings` action) also creates an `EmbeddingBackfillRun` row recording the enqueued baseline, so the admin badge can show per-backfill progress. At most one backfill is active at a time — a second invocation refuses while one is running (abandoned runs are auto-closed, §6.8).

The three scenarios still apply:

1. **Backfill** of historical NULLs (rows loaded before the deferred-embedding architecture shipped).
2. **Dim or model change** following §4.5 (or `ReportSearchIndex.objects.update(embedding=None)` for a same-dim model swap).
3. **Outage recovery** for tasks that exhausted Procrastinate retries during an extended embedding-service outage.

Properties:

- **Idempotent.** Filter is `embedding IS NULL`; re-runs are no-ops on already-drained rows.
- **Resumable.** No checkpoint state. Killed mid-run → re-run picks up remaining NULLs.
- **Rate-limited.** The worker's `--concurrency K` caps concurrent embedding HTTP calls regardless of how many tasks the command enqueues. Operators cannot accidentally hammer the embedding service.
- **Visible.** Enqueued tasks appear in the standard Procrastinate observability surface (admin, logs, telemetry). Failed retries surface there as well.

### 6.6 `PGSEARCH_SYNC_INDEXING` retained; ordering enforced by chaining

The pre-existing `PGSEARCH_SYNC_INDEXING` switch is **retained** with the same semantics it had before hybrid search: it controls whether FTS bulk-indexing runs inline on the request thread or is deferred to a `bulk_index_reports` Procrastinate task. Pgsearch's `_index_reports` handler reads the flag and dispatches accordingly:

| Mode | `PGSEARCH_SYNC_INDEXING` | FTS step | Embedding step |
|---|---|---|---|
| Sync | `True` | `bulk_upsert_report_search_indexes(ids)` inline inside the handler | `enqueue_embed_reports(ids)` immediately after, in the same handler call. RSI rows are already committed. |
| Deferred (default) | `False` | `enqueue_bulk_index_reports(ids)` defers `bulk_index_reports` to the `default` queue | `bulk_index_reports` itself calls `enqueue_embed_reports` at the end of its run. Handler does *not* enqueue embed directly. |

`bulk_index_reports` ends with `enqueue_embed_reports(report_ids)`. The enqueue happens inside the same task body, after `bulk_upsert_report_search_indexes` has committed the RSI rows, so the embeddings worker can only observe a `report_ids` payload whose RSI rows already exist. This replaces the earlier "defensive idempotent re-upsert at the top of the embed task" design — the chain is the ordering guarantee. (As a belt-and-braces measure, the embed task also logs a WARNING and skips any id that has no RSI row, rather than crashing.)

Properties:

- **No race.** The embeddings worker never picks up a report id before its RSI row is committed. The embed task can read `report.body` and write `embedding` without checking for RSI existence.
- **Simple embed task.** No `bulk_upsert_report_search_indexes` shim at the top, no idempotent re-upsert cost on the embeddings worker, no extra commit hop.
- **Operator choice preserved.** Deployments that prefer sync FTS keep that option; deployments that prefer the deferred FTS task for large bulks keep that option. Hybrid search is orthogonal to the FTS-mode decision.
- **Two queues, two concerns.** FTS deferral runs on the `default` queue (where `bulk_index_reports` already lived); embedding runs on the dedicated `embeddings` queue. FTS-only worker capacity does not compete with embedding capacity.
- **Operator-triggered re-embed.** The `embed_pending` management command and the `enqueue_pending_embeddings` admin action call `enqueue_embed_reports` directly (at backfill priority). Both bypass `bulk_index_reports` but the invariant still holds: their queries are over existing `ReportSearchIndex` rows with `embedding IS NULL`, so the RSI rows exist by construction.

The single-create / PUT path is unaffected by `PGSEARCH_SYNC_INDEXING`. Its FTS step is the `post_save` signal on `Report`, which is always sync inline by construction. The same handler still fires for it; the handler then takes the sync-mode branch's behaviour (immediate embed defer), which is correct since the RSI row was just written sync by the signal.

### 6.7 Sync DRF; no async views required

The enqueue (`enqueue_embed_reports(...)`, which drives `configure_task(...).defer(...)`) is a synchronous Procrastinate API call, so the report views remain plain sync DRF (`ReportViewSet`, unchanged in shape from main). No `await` lives inside any request handler. The async-view rewrite proposed in PR #230 is **not a dependency** of this design and is intentionally not pulled in — the entire embedding workload lives on the worker side, behind the `embeddings` queue.

### 6.8 Pipeline observability: report-centric badge and backfill runs

The `ReportSearchIndex` changelist badge leads with reports (what operators care
about) and relegates Procrastinate mechanics to a muted secondary line:

```
Embedding pipeline
1456 / 4077 reports processed · 2000 queued · 500 in progress · 121 not queued
Backfill: 500 / 2000 reports processed (25%) · started 12 min ago
subjobs: 4 queued · 1 in-flight · 0 failed (embeddings queue)  [Cancel queued backfill]
```

**Primary line (global, no new state).** `embedded / total reports processed`,
where `total = ReportSearchIndex.objects.count()` and `embedded = total −
pending` (`pending` = `embedding IS NULL`). While `pending > 0`, the remainder
is broken down (zero-valued segments omitted):

- *queued* / *in progress* — reports covered by `todo` / `doing` subjobs, summed
  DB-side via `jsonb_array_length(args->'report_ids')` grouped by status (the id
  arrays never leave Postgres). "In progress" is exact, not approximate: a
  subjob bulk-writes its embeddings only at completion, so none of a `doing`
  job's reports are embedded yet.
- *not queued* — `max(0, pending − queued − in progress)`: NULL rows no live job
  covers (retry exhaustion, cancelled backfills, failed subjobs — failed jobs'
  reports intentionally count here since they need re-enqueueing). This is the
  "run `embed_pending`" signal. Clamped because the counts aren't one snapshot.

The idle-and-done state is just `4077 / 4077 reports processed`.

**Backfill line (per-run state).** `EmbeddingBackfillRun` rows track individual
backfills — necessary because completed jobs are deleted from
`procrastinate_jobs`, so a drain-scoped fraction is not derivable from live
queue state. Fields: `started_at`, `finished_at` (null), `cancelled_at` (null),
`total_reports`, `processed_reports` (default 0), `triggered_by`
(`embed_pending` or the admin username). A run is *active* while both end
timestamps are NULL. Migration `0003_embeddingbackfillrun`.

- **Creation — one active backfill at a time:** `embed_pending` and the admin
  `enqueue_pending_embeddings` action create a run with `total_reports =
  len(report_ids)` and thread `run_id` through `enqueue_embed_reports` into
  each subjob's task args. Write-path (live-priority) enqueues carry no run.
  Both entry points **refuse to start while a run is active** ("Backfill
  already active: 500/2000 processed. Cancel it first with `embed_cancel`.") —
  with one escape hatch: an active run with zero live subjobs and `processed <
  total` is *abandoned* (jobs lost to retry exhaustion or a dead worker); the
  next invocation auto-closes it (stamps `cancelled_at`, logs the takeover)
  and proceeds, so a wedged run can never block future backfills.
- **Progress:** after its successful bulk-write, `embed_reports_task` increments
  `processed_reports` atomically (`F() + n`), then flips `finished_at` when
  `processed ≥ total`. Failed subjobs never increment. Counter-based progress
  is immune to the worker's `--delete-jobs` policy, unlike deriving progress
  from surviving job rows.
- **Cancel:** `cancel_backfill_embeddings()` stamps `cancelled_at` on active
  runs, freezing their fraction in history.
- **Stall detection:** for the active run, the badge counts live jobs whose args
  carry its `run_id`; zero live jobs with `processed < total` renders a
  "stalled — no live subjobs" marker instead of implying progress (the
  dead-worker scenario: worker crashes are otherwise invisible here because the
  container stays "Up").
- **Display:** the badge shows *the* active run (single-active makes "latest"
  unambiguous); finished/cancelled runs are visible in the run history — a
  read-only `EmbeddingBackfillRun` admin listing.
- **Terminology:** with the run as the explicit parent, *subjob* keeps its
  meaning (a sub-unit of the backfill, mirroring core's `AnalysisJob` →
  `AnalysisTask` shape); `EMBEDDING_SUBJOB_SIZE`, `--subjob-size`, and all
  operator messages keep their names.

**Secondary line.** Subjob counts by status plus the queue name, rendered only
when any count is nonzero; `failed` keeps its red highlight; the cancel-backfill
button keeps its existing `todo_backfill > 0` visibility rule.

## 7. Hybrid search provider

### 7.1 Universe and fusion

The hybrid result universe is the **union** of two filter-bounded candidate sets:

- **Vector top-K:** the `HYBRID_VECTOR_TOP_K` nearest rows by cosine distance to the query embedding, filtered by structured filters and `embedding IS NOT NULL`. *Not* constrained to the FTS hit set.
- **FTS hits:** all rows matching the tsquery and the structured filters, capped at `HYBRID_FTS_MAX_RESULTS`.

A report appears in results if it is in **either** set. This is the change from the earlier draft, made because radiology queries like "no pneumothorax" must be able to surface reports that lexically don't match (the GIN index drops "no" as a stop word) but are semantically related.

Each report's score is plain Reciprocal Rank Fusion:

```
score(d) = (1 / (HYBRID_RRF_K + vec_rank[d])  if d ∈ vec_top_K  else 0)
         + (1 / (HYBRID_RRF_K + fts_rank[d])  if d ∈ fts_hits   else 0)
```

Properties:

- Reports in both sides outrank reports in only one side (sum of two terms vs. one).
- Vector contribution decays after rank K (no `vec_rank` entry), so the ordering naturally transitions from "hybrid head" to "FTS tail" with no explicit cutoff.
- A query with zero FTS hits returns `vec_top_K` ranked by vector position only — pure semantic search.
- A query with embedding failure returns FTS hits ranked by `ts_rank` only — the pre-hybrid behavior.

### 7.2 `search()` flow

```python
def search(search: Search) -> SearchResult:
    query_str = _build_query_string(search.query)
    language = _resolve_language(search.filters)
    filter_query = _build_filter_query(search.filters)
    tsquery = SearchQuery(query_str, search_type="raw", config=language)

    # Vector side: strip NOT branches before embedding (see §7.8). If nothing
    # is left (e.g., the query was just `NOT X`), skip the embedding call
    # entirely and fall through to FTS-only.
    query_text = QueryParser.unparse_for_embedding(search.query)
    query_vec: list[float] | None = None
    if query_text.strip():
        query_vec = _embed_query_or_none(query_text, "Hybrid search")

    vec_rank: dict[int, int] = {}
    vec_distance: dict[int, float] = {}
    if query_vec is not None:
        vec_rows = list(
            ReportSearchIndex.objects.filter(filter_query)
            .distinct()
            .exclude(embedding__isnull=True)
            .annotate(distance=CosineDistance("embedding", query_vec))
            .order_by("distance", "report_id")
            .values_list("report_id", "distance")[: settings.HYBRID_VECTOR_TOP_K]
        )
        for i, (rid, dist) in enumerate(vec_rows):
            vec_rank[rid] = i + 1
            vec_distance[rid] = float(dist)

    # FTS side: bounded set, ts_rank only (no headline at this stage).
    fts_rows = list(
        ReportSearchIndex.objects.filter(filter_query)
        .distinct()
        .filter(search_vector=tsquery)
        .annotate(rank=SearchRank(F("search_vector"), tsquery))
        .order_by("-rank", "report_id")
        .values("report_id", "rank")[: settings.HYBRID_FTS_MAX_RESULTS]
    )
    fts_rank = {row["report_id"]: i + 1 for i, row in enumerate(fts_rows)}

    # Fusion (pure Python; rrf_fuse lives in utils/fusion.py for unit testing).
    # Returns ordered (report_id, rrf_score) pairs.
    ordered_pairs = rrf_fuse(vec_rank, fts_rank, k=settings.HYBRID_RRF_K)
    rrf_score_by_id = dict(ordered_pairs)
    ordered_ids = list(rrf_score_by_id)

    total_count = len(ordered_ids)
    total_relation = (
        "at_least"
        if len(fts_rows) >= settings.HYBRID_FTS_MAX_RESULTS
           or len(vec_rank) >= settings.HYBRID_VECTOR_TOP_K
        else "exact"
    )
    # limit=None means "everything from offset" (retrieval providers use it).
    if search.limit is None:
        page_ids = ordered_ids[search.offset :]
    else:
        page_ids = ordered_ids[search.offset : search.offset + search.limit]

    # Headline + hydration for the page slice only
    page_rows = (
        ReportSearchIndex.objects.filter(report_id__in=page_ids)
        .annotate(
            summary=SearchHeadline("report__body", tsquery, config=language,
                                   start_sel="<em>", stop_sel="</em>",
                                   min_words=10, max_words=20, max_fragments=10),
            rank=SearchRank(F("search_vector"), tsquery),
        )
        .select_related("report")
    )
    by_id = {r.report.pk: r for r in page_rows}

    documents: list[ReportDocument] = []
    for rid in page_ids:
        rsv = by_id.get(rid)
        if rsv is None:
            continue  # report deleted between fusion and hydration
        rsv.summary = summary_with_fallback(rsv.report.body, rsv.summary or "", max_words=30)
        documents.append(
            document_from_pgsearch_response(
                rsv,
                cosine_distance=vec_distance.get(rid),
                rrf_score=rrf_score_by_id.get(rid, 0.0),
            )
        )

    return SearchResult(total_count=total_count, total_relation=total_relation, documents=documents)
```

Embedding failures never break search: `_embed_query_or_none` wraps `EmbeddingClient().embed_query(...)` and returns `None` on any failure — `logger.exception` for permanent misconfiguration (`EmbeddingClientError` plus the typed `_PERMANENT_EMBEDDING_ERRORS`: `AuthenticationError`, `PermissionDeniedError`, `NotFoundError`, `BadRequestError`) and `logger.warning` for load-shaped conditions (`RateLimited`, any other `openai.OpenAIError`). `None` skips the vector side and the query degrades to FTS-only.

### 7.3 Empty-summary fallback

`SearchHeadline` returns an empty string when the document body has no FTS match (the vector-only hit case). `summary_with_fallback` (`radis/pgsearch/utils/fusion.py`) replaces an empty summary with the first 30 words of `report.body`. Trivial helper, ~5 lines.

### 7.4 `retrieve()`

Same fusion logic, returns an iterator of `report__document_id` in `ordered_ids` order. No headline. Used by `ExtractionJob` and `SubscriptionJob` to walk the matching id set.

### 7.5 `count()` and `filter()`

Unchanged. These operate on filters only and never call the embedding service.

### 7.6 `ReportDocument` score fields

`ReportDocument` (`radis/search/site.py`) carries three score fields. The
existing `relevance` is preserved for API backwards compatibility; two new
fields are added so callers (and the UI) can see *why* a result ranked where
it did:

```python
class ReportDocument(NamedTuple):
    relevance: float | None                  # FTS ts_rank — existing; 0.0 for vector-only hits
    document_id: str
    # ...
    cosine_distance: float | None = None     # NEW — pgvector cosine distance; None for FTS-only hits
    rrf_score: float = 0.0                   # NEW — the value the final ordering is based on
```

Semantics:

- `relevance` — Postgres `ts_rank` of the row's `search_vector` against the
  tsquery. Same field/shape pre- and post-hybrid; callers that read it
  continue to work. Defaults to `0.0` for documents that came from the vector
  half only.
- `cosine_distance` — the `CosineDistance("embedding", query_vec)` annotation
  for rows that made `vec_top_K`. `None` for FTS-only hits and whenever the
  query path skipped vector retrieval (embedding service down, or the query
  reduced to `NOT` after §7.8 stripping).
- `rrf_score` — the fused score from §7.1; this is what the result ordering
  is based on. Exposed for transparency, debugging, and UI display
  (operators can see at a glance which side contributed). Also useful when
  the §11.6 re-ranker lands: it will read `rrf_score` to seed its top-N
  candidate selection.

All three fields are populated by `document_from_pgsearch_response` during
the page-slice hydration step in §7.2. The hydration query annotates the page
rows with `ts_rank`, looks up the corresponding entries in the `vec_rank` /
`fts_rank` / `rrf` dicts, and assembles the document.

### 7.7 `search_provider.max_results`

Updated to `max(HYBRID_VECTOR_TOP_K, HYBRID_FTS_MAX_RESULTS)`, which is what the `SearchView` page-bound check uses to reject impossibly-deep pagination.

### 7.8 Negation-aware query for embedding

Dense embedding models are polarity-blind: the vector for `"NOT pneumothorax"`
clusters near the vector for `"pneumothorax"`, so the top-K nearest neighbours
to a `NOT X` query are documents *about* X — the polar opposite of what the
user asked for. The FTS half handles `NOT X` correctly (it returns docs
without X), so when both halves are fused naively the vector half pollutes
the candidate pool with anti-matches.

The fix is upstream of embedding: strip negated branches from the query string
before sending it to the embedding model. The FTS side still receives the
full structured query, so its negation semantics are preserved.

A new static method on `QueryParser` walks the AST and emits a stripped
string. The shape mirrors the existing `QueryParser.unparse` walker:

```python
@staticmethod
def unparse_for_embedding(node: QueryNode) -> str:
    """Like unparse(), but drops the operand of every UnaryNode("NOT", X)
    and collapses any BinaryNode whose children both become empty.
    Returns the empty string if the whole query reduces to NOT clauses."""
    if isinstance(node, TermNode):
        # Same as unparse: emit the term verbatim (PHRASE keeps quotes).
        return QueryParser.unparse(node)
    if isinstance(node, ParensNode):
        inner = QueryParser.unparse_for_embedding(node.expression)
        return f"({inner})" if inner else ""
    if isinstance(node, UnaryNode):
        # The only unary operator in the grammar is NOT — drop the operand.
        return ""
    if isinstance(node, BinaryNode):
        left = QueryParser.unparse_for_embedding(node.left)
        right = QueryParser.unparse_for_embedding(node.right)
        if not left and not right:
            return ""
        if not left:
            return right
        if not right:
            return left
        if node.implicit:
            return f"{left} {right}"
        return f"{left} {node.operator} {right}"
    raise ValueError(f"Unknown node type: {type(node)}")
```

Outcomes:

| User query | `unparse()` (FTS path) | `unparse_for_embedding()` (vector path) | Behavior |
|---|---|---|---|
| `pneumothorax` | `pneumothorax` | `pneumothorax` | Both halves agree; RRF amplifies. |
| `A AND NOT B` | `A AND NOT B` | `A` | Vector embeds the positive concept; FTS enforces the exclusion. |
| `NOT X` | `NOT X` | `""` | Vector path skipped (see §7.2); FTS-only ranking. |
| `(A AND NOT B) OR C` | `(A AND NOT B) OR C` | `(A) OR C` | Empty NOT branch collapses; surviving structure retained for vector. |

The method does not attempt to resolve OR-asymmetry or other operator
mismatches documented in §11.5 — those remain open trade-offs in the design.
This is a targeted fix for the `NOT` case, which is the most acute failure
mode for radiology queries.

## 8. Configuration

### 8.1 Env-driven (per-deployment, set in `.env`)

```python
# radis/settings/base.py
EMBEDDING_PROVIDER_URL     = env.str("EMBEDDING_PROVIDER_URL", default="")
EMBEDDING_PROVIDER_API_KEY = env.str("EMBEDDING_PROVIDER_API_KEY", default="")
EMBEDDING_MODEL_NAME       = env.str("EMBEDDING_MODEL_NAME", default="Qwen/Qwen3-Embedding-4B")
EMBEDDING_DIM              = env.int("EMBEDDING_DIM", default=1024)

EMBEDDING_REQUEST_TIMEOUT  = env.int("EMBEDDING_REQUEST_TIMEOUT", default=30)  # seconds
EMBEDDING_BATCH_SIZE       = env.int("EMBEDDING_BATCH_SIZE", default=200)      # texts per HTTP call
EMBEDDING_SUBJOB_SIZE      = env.int("EMBEDDING_SUBJOB_SIZE", default=1000)    # reports per subjob
```

These vary across deployments and are operator-controlled. `EMBEDDING_DIM` is intentionally an env decision because it is schema-coupled (see §4.5 and the `pgsearch.E001` check). Timeout, batch size, and subjob size are env because they track the deployed provider's capacity (a self-hosted vLLM box and a rate-limited gateway want very different values). There is no `EMBEDDING_BACKEND` / `EMBEDDING_PROVIDER_PATH` — since the openai-SDK rewrite there is exactly one wire shape, an OpenAI-compatible `/v1` endpoint (§5.3). Worker concurrency is set in the compose command line: hardcoded `--concurrency 4` in dev, `${EMBEDDINGS_WORKER_CONCURRENCY:-2}` in prod.

### 8.2 Code constants (tuning knobs, in `base.py`)

```python
EMBEDDING_QUERY_INSTRUCTION = (
    "Instruct: Given a radiology search query, retrieve relevant radiology reports.\nQuery: "
)

# Procrastinate priorities on the `embeddings` queue: live writes outrank backfill.
EMBEDDING_LIVE_PRIORITY = 1
EMBEDDING_BACKFILL_PRIORITY = 0

# Rate-limit gate (mirrors the LLM_RATE_LIMIT_* semantics; separate provider, separate gate)
EMBEDDING_RATE_LIMIT_BACKOFF_BASE_SECONDS = 2.0
EMBEDDING_RATE_LIMIT_BACKOFF_MAX_SECONDS = 120.0
EMBEDDING_RATE_LIMIT_HEADER_CEILING_SECONDS = 1800.0
EMBEDDING_RATE_LIMIT_MAX_WAIT_SECONDS = 300.0        # batch budget
EMBEDDING_RATE_LIMIT_QUERY_MAX_WAIT_SECONDS = 10.0   # query budget (user is waiting)

# Local transient retries (non-429): N retries after the first call, backoff 0.5s, 1s
EMBEDDING_TRANSIENT_RETRY_ATTEMPTS = 2
EMBEDDING_TRANSIENT_RETRY_BASE_SECONDS = 0.5

# Procrastinate subjob retry: waits 6s, 36s, ~4min, ~22min
EMBEDDING_TASK_MAX_ATTEMPTS = 5
EMBEDDING_TASK_EXPONENTIAL_WAIT_SECONDS = 6

HYBRID_VECTOR_TOP_K = 100
HYBRID_FTS_MAX_RESULTS = 10_000
HYBRID_RRF_K = 60
```

These are tuning constants. Changing them is a code change with a PR diff. This matches the project's existing pattern (`EXTRACTION_LLM_CONCURRENCY_LIMIT = 6`, the `CHAT_*_SYSTEM_PROMPT` blocks).

### 8.3 `example.env`

Documents the `EMBEDDING_PROVIDER_URL` / `EMBEDDING_PROVIDER_API_KEY` / `EMBEDDING_MODEL_NAME` / `EMBEDDING_DIM` keys with an Ollama dev recipe (`http://host.docker.internal:11434/v1`, api key unused) as commentary — one OpenAI-compatible shape, no backend switch.

### 8.4 Compose

`docker-compose.base.yml`:

- The `EMBEDDING_PROVIDER_URL`, `EMBEDDING_PROVIDER_API_KEY`, `EMBEDDING_MODEL_NAME`, `EMBEDDING_DIM`, `EMBEDDING_REQUEST_TIMEOUT`, `EMBEDDING_BATCH_SIZE`, `EMBEDDING_SUBJOB_SIZE` env keys are visible to all services via the shared env plumbing.
- New service `embeddings_worker` inheriting `*default-app` runs `./manage.py bg_worker -q embeddings` (see §6.3).

`docker-compose.dev.yml` / `docker-compose.prod.yml`:

Both add an `embeddings_worker.command` block. Dev uses `-l debug --autoreload --concurrency 4`; prod uses `--concurrency ${EMBEDDINGS_WORKER_CONCURRENCY:-2}`.

## 9. Error handling and degradation

| Failure | Behavior | Logging |
|---|---|---|
| Embedding service returns 5xx/timeout/429 during query-time | `query_vec = None` (`_embed_query_or_none`); result list ordered by FTS-only; request succeeds | WARNING |
| Embedding service returns 4xx during query-time (auth, bad model, bad request) | Same FTS-only fallback; treated as misconfiguration | ERROR with traceback (`logger.exception`) |
| Embedding service returns malformed body (count/dim mismatch) | `EmbeddingClientError` raised; query falls back to FTS-only | ERROR with traceback |
| Embedding service degraded/down during `embed_reports_task` | Local transient retries (0.5 s, 1 s) → if exhausted, the error escapes and `EMBEDDING_TASK_RETRY_STRATEGY` retries the whole subjob (6 s, 36 s, ~4 min, ~22 min). **API request was never affected** (embedding is always deferred). | WARNING per local retry; ERROR when the task raises |
| Subjob fails after Procrastinate retries exhausted | Job row ends `failed` in `procrastinate_jobs` with `report_ids` in its payload. Embeddings stay NULL; hybrid search silently returns those reports via FTS only. Operator runs `embed_pending` after fixing the cause. | ERROR on final failure; Procrastinate admin shows the failed job |
| Report body exceeds embedding model's context window | Backend returns 400 → `openai.BadRequestError`. In no retry set (deterministic), so the whole subjob fails permanently; blast radius is one subjob. No per-report bisect/skip since 2026-07-02 (§6.2). Operator fixes the report or the model's context window, then `embed_pending`. | ERROR from Procrastinate with the ids in the job row |
| Report deleted between enqueue and execution | RSI row is CASCADE-deleted with the report; the task logs the missing ids and embeds the rest of the subjob | WARNING with truncated id list |
| Wrong-dim vector returned by backend | Client-side validation in `_normalize_response`: too-small raises `EmbeddingClientError` (retried, then subjob fails); too-large is Matryoshka-truncated to `EMBEDDING_DIM` and renormalized (§5.4) | ERROR on failure path |
| `EMBEDDING_PROVIDER_URL` empty | `EmbeddingClient.__init__` raises `EmbeddingClientError` at the call site. Query path falls back to FTS-only per request; embed subjobs burn their retries and fail. `embed_pending` after fixing settings. | ERROR with traceback per query; ERROR on task failure |
| `EMBEDDING_DIM` ≠ migration dim | `pgsearch.E001` system check blocks startup — caught at deploy time, not runtime | system check output |

**Deliberate non-policies:**

- The product never fails a search request because the embedding service is down. It degrades to FTS-only.
- Query embeddings are not cached. The complexity and freshness trade-off is not worth it at the corpora sizes RADIS targets.
- `EmbeddingClient` does not retry internally. The worker path wraps the client call in `_embed_chunk_with_retry` — the rate-limit gate outermost (429s, one 300 s budget per call), `with_transient_retries` inside (brief blips, up to 3 total calls) — and lets Procrastinate's task-level retry handle anything that escapes. The query path runs a single shot through the same gate with the short query budget (10 s) and falls back to FTS-only on any failure (`RateLimited`, `EmbeddingClientError`, or typed SDK errors).

**Observability:**

- `embed_reports_task` logs at INFO on start (`reports=N`) and finish (`embedded=N duration_ms=D`); `enqueue_embed_reports` logs subjob count and priority; each local transient retry logs a WARNING (attempt, wait, error) from the shared `with_transient_retries` helper.
- Query-path fallbacks log per §9 table above (WARNING for load, ERROR for misconfiguration) — a silently degraded search is the failure mode this guards against.
- The existing OpenTelemetry overlay (commit `653e0c67`) tags telemetry per service; embedding spans show up under the `embeddings_worker` service.

## 10. Testing strategy

### 10.1 Unit-ish tests (mock transport, minimal DB)

All tests live in `radis/pgsearch/tests/` (the project has no separate `tests/unit` tree). Shared helpers for the gate and retry stack are covered in `radis/core/tests/test_rate_limit.py`.

| File | Coverage |
|---|---|
| `test_embedding_client.py` | Request/response round-trip over `httpx.MockTransport`, instruction prefix on `embed_query`, L2 normalization, dim validation (too-small raises, larger truncated via Matryoshka), count mismatch, missing URL, typed SDK errors passing through unwrapped |
| `test_fusion.py` | `rrf_fuse(vec_rank, fts_rank, k)` pure-Python helper: disjoint, overlapping, FTS-only, vector-only, both-empty, tiebreak by report_id; `summary_with_fallback` |
| `test_embed_reports_task.py` | Loads RSI rows by report_id, calls `embed_documents`, bulk-updates vectors; internal batching by `EMBEDDING_BATCH_SIZE`; retry stack (transient-then-success retried locally, `EmbeddingClientError` retried, 429 goes to the gate not the local retries, `RateLimited` propagates to Procrastinate); `enqueue_embed_reports` chunking + priorities; `bulk_index_reports` chaining; `cancel_backfill_embeddings` |
| `test_apps_checks.py` | `pgsearch.E001` / `pgsearch.E002` system checks |
| `test_embed_pending_command.py` | `embed_pending` selects NULL-embedding rows, honours `--subjob-size` / `--limit`, enqueues at backfill priority |
| `test_admin.py` | Admin actions (`enqueue_pending_embeddings`, `clear_embeddings`), cancel-backfill view, pipeline stats |

### 10.2 Integration tests (real Postgres + pgvector)

| File | Coverage |
|---|---|
| `test_provider_hybrid.py` | FTS-only hit, vector-only hit, both-sides-ranks-first, embedding-failure fallback (typed 429, `RateLimited`, generic), NULL-embedding rows still returned via FTS, empty-summary fallback, `retrieve()` ordering + fallback, `cosine_distance`/`rrf_score` on documents, M2M filter dedup, §7.8 NOT-stripping (skip embed on pure-NOT, embed positive branch only) |
| `test_providers.py`, `test_indexing.py`, `test_language_utils.py` | Pre-hybrid FTS provider behavior, RSI upsert path, language resolution — retained and passing against the renamed model |

The squashed `0002_hybrid_search` migration (extension, rename, vector column, HNSW, partial index) is exercised implicitly by the test database build; there is no dedicated `django-test-migrations` suite.

Fixtures: tests create reports via `ReportFactory` (signals create the RSI rows) and then write deterministic unit vectors directly (`ReportSearchIndex.objects.filter(report=r).update(embedding=_unit_vec(seed, dim))`). Real Qwen3 embeddings are not used in tests.

### 10.3 View-level smoke

`radis/search/tests/test_views.py`: `test_search_view_returns_200_when_embedding_provider_unset` — SearchView returns 200 via the FTS-only fallback when `EMBEDDING_PROVIDER_URL` is unset. Hybrid ranking itself is covered at the provider layer (§10.2), not through the view.

### 10.4 Acceptance

No hybrid-specific acceptance test exists yet; the existing acceptance suite (`@pytest.mark.acceptance`) exercises the search page against the dev containers as before.

### 10.5 Explicitly not tested

- Live Qwen3 retrieval quality (offline eval, out of scope).
- pgvector HNSW recall under specific data shapes (extension's responsibility).
- Wire formats beyond the single OpenAI-compatible `/v1` shape.

## 11. Known limitations and future work

### 11.1 Negation / polarity (the "no pneumothorax" problem)

Dense embedding models — including Qwen3-Embedding — embed semantically opposite phrases close together. "No pneumothorax" and "pneumothorax present" produce nearby vectors, so the vector half of the hybrid score is *polarity-blind*. The FTS half partly compensates by allowing the user to construct explicit AND-NOT queries, but Postgres' GIN index drops "no" as a stop word, so a naive query like `no pneumothorax` is effectively `pneumothorax` on the FTS side.

This is a real concern for radiology, where negated findings are pervasive ("no acute …", "no evidence of …", "no significant …"). **Hybrid search as designed here does not solve this.** It is documented as an accepted limitation of v1, and a v2 conversation should address it.

Candidate solutions to evaluate in a future spec (none committed):

- A cross-encoder re-ranker over the top-N hybrid results (e.g., a small instruction-tuned model that knows to score "no X" against "X present" as opposite).
- Adding a sparse/late-interaction model (SPLADE, ColBERT) alongside the dense vector — sparse models preserve token-level polarity.
- Negation-aware query preprocessing: detect negation, route to a different retrieval mode, or expand to phrasal `AND-NOT` clauses on the FTS side that bypass the stop-word filter (e.g., search the raw body, not the tsvector).
- Structured-findings indexing: have the LLM extract presence/absence flags per finding category at ingest time, search those structured fields instead of (or in addition to) prose.

### 11.2 Dimension changes are manual

See §4.5.

### 11.3 GGUF dev embeddings ≠ bf16 prod embeddings

Documented in §5.4. Mitigated by following §4.5 after a model swap and then running `./manage.py embed_pending` (§6.5), which enqueues `embed_reports_task` for every NULL row; the embeddings worker drains the queue at its configured concurrency.

### 11.4 No body-change detection for re-embedding

V1 re-embeds anything where `embedding IS NULL`. A future optimization could
track whether the body actually changed (e.g., a `body_hash` column on
`ReportSearchIndex` updated only on body changes) so metadata-only updates
don't have to null the embedding. Not in v1; profiling will tell us whether it
matters.

### 11.5 Operator-aware queries: residual FTS / vector asymmetry

Both halves of hybrid search receive a derivation of the same parsed `QueryNode`, but interpret it through completely different machinery. The FTS side consumes a `tsquery` built by `_build_query_string` where `AND`, `OR`, `NOT`, quoted phrases, and parens are first-class boolean operators (`&`, `|`, `!`, `<->`, `()`). The vector side consumes a string derived from the AST by `QueryParser.unparse_for_embedding` (§7.8) and feeds it to the embedding model as natural language; the remaining operators become ordinary word tokens that the model has no operator-aware machinery to interpret.

Practical consequences after the §7.8 NOT-stripping fix:

- **Natural-phrase queries** (`pneumothorax`, `chest x-ray`, implicit-AND `cardiac arrest`) — both halves point the same direction. RRF amplifies the agreement. This is the workload hybrid search is best at.
- **`A AND B`** — FTS strictly intersects; vector returns docs about a topic-mix of A and B. Docs matching both lexically *and* semantically rank highest, which is the desired outcome. Vector contributes useful expansion but not boolean precision.
- **`A OR B`** — FTS unions; the vector half has no concept of disjunction and just produces a centroid-style embedding. Docs about either A or B that happen to be near the centroid still get retrieved, but a doc purely about A may not appear unless it's also close to the centroid. **Open trade-off.** Vector half degrades from "asset" to "noise" for OR-heavy queries; no fix in this spec.
- **`NOT X` / `A AND NOT B`** — addressed by §7.8. Vector embeds only the positive branches; FTS enforces the negation; the halves are aligned.

The asymmetry is real and remains a quality consideration for OR-heavy queries. The §11.6 cross-encoder re-ranker, when added, can sharpen the head of results but cannot fix a polluted candidate pool — see the analysis at the end of this section for why upstream stripping (the §7.8 approach for `NOT`) is the architecturally correct order of operations.

**Why a re-ranker alone cannot fix recall problems.** A cross-encoder re-ranker improves precision *within the candidate pool it is given* — it cannot improve recall of that pool. If a polarity-blind vector half had poisoned a `NOT pneumothorax` pool with ~100 anti-matches, re-ranking the top-20 would sharpen the head but ~590 correct docs would still live below the re-ranker's cutoff at their original RRF positions. The architecturally correct order is to fix recall upstream (§7.8) and *then* layer a re-ranker for precision (§11.6). A re-ranker without the upstream fix is rearranging deck chairs on a polluted pool.

### 11.6 Cross-encoder re-ranker (deferred)

A planned follow-up adds a re-ranker stage between hybrid fusion and result hydration to lift precision (especially on operator-light natural-phrase queries, where the candidate pool is already correct but RRF ordering is mediocre) and to partially compensate for §11.1's polarity blindness. Two backend patterns are under consideration:

- **Pointwise cross-encoder via vLLM.** Qwen3-Reranker-4B served with `vllm serve … --task score` exposes `/v1/rerank` (Cohere/TEI shape: `{model, query, documents}` → `[{index, relevance_score}]`). Logit-based scoring (yes/no token logits → softmax) gives graded relevance in [0,1]. Latency ~30–100 ms per pair on a single GPU; for top-20 candidates that's ~0.5–1.5 s added.
- **Listwise LLM re-ranker** via the existing OpenAI-compatible chat-completions endpoint. The LLM is prompted with the query and the top-N candidates packed into a single message; structured output (`response_format=json_object`) returns a ranked list of indices. One HTTP call per query rather than N. Latency ~1–3 s for top-20 depending on model size. Quality trades off graded precision for the LLM's strong instruction-following — particularly the explicit "respect negation" cue, which the pointwise reranker has to learn implicitly.

vLLM is the recommended production host for the pointwise path because Ollama (as of mid-2025) does not expose token logits cleanly, which collapses Qwen3-Reranker to a binary 1.0/0.0 signal and loses graded ordering. Ollama can still serve the LLM listwise backend without issue.

### 11.7 Evaluation strategy for the layered hybrid stack

Six profiles cover the additive layers:

| Profile | Negation strip (§11.5) | Re-ranker (§11.6) |
|---|---|---|
| `baseline` | off | off |
| `strip` | on | off |
| `rerank-qwen` | off | Qwen3-Reranker via vLLM |
| `rerank-llm` | off | listwise LLM |
| `both-qwen` | on | Qwen3-Reranker via vLLM |
| `both-llm` | on | listwise LLM |

A `run_search_eval` management command loops a set of test queries through all six profiles (toggling settings via `override_settings`) and dumps comparable JSON output with top-N docs, per-layer scores (`ts_rank`, `cosine_distance`, `rrf_score`, `rerank_score`), and per-profile latencies.

**Labeling.** Per-pair LLM relevance judgment ("is doc D relevant to query Q?") is unreliable for radiology because (a) it inherits the same polarity blind spot the system is trying to evaluate, and (b) it introduces circular bias when the labeling LLM and re-ranker LLM share a family. The preferred approach is *concept-based polarity-aware labeling*: label each report once per clinical concept with `PRESENT` / `ABSENT` / `NOT_MENTIONED`, then derive query relevance deterministically (`pneumothorax` → `PRESENT ∪ ABSENT`; `NOT pneumothorax` → `NOT_MENTIONED ∪ ABSENT` for strict exclusion, or `ABSENT` only for "rule-out" semantics). The concept labels are reusable across many queries and survive prompt/model changes. The upstream label-filter work in PR #196 produces structured labels with comparable semantics and is the intended source of ground truth for production-scale evaluation.

## 12. Rollout plan

1. **Schema + dep.** `pgvector` pip dep + `0002_hybrid_search` migration (extension + embedding column + HNSW). No behaviour change yet.
2. **Embedding client + tests.** Land `EmbeddingClient` (sync, over the openai SDK — used by both the query side and the worker side). No callers wired up yet.
3. **Worker + task + queue.** Add `embeddings_worker` container (compose), the sync `embed_reports_task` on the `embeddings` queue, and the worker command with an explicit `--concurrency`. Without callers, the worker stays idle.
4. **Write-path enqueue.** Wire the `_index_reports` handler to call `enqueue_embed_reports(report_ids)`. The bulk-upsert path keeps both `PGSEARCH_SYNC_INDEXING` modes (§6.6); the sync mode enqueues embedding immediately after FTS, the deferred mode chains embedding at the tail of `bulk_index_reports`. From this point on, **every write enqueues embedding subjobs**; the embeddings worker drains the queue.
5. **Provider switch.** Replace the body of `radis.pgsearch.providers.search()` and `retrieve()` with the hybrid implementation. Rows still missing an embedding participate via the FTS half only.
6. **(Optional) historical backfill.** Run `./manage.py embed_pending` to enqueue `embed_reports_task` subjobs (at backfill priority) for every existing NULL row. Same command serves outage recovery and dim/model-change scenarios (§6.5).
7. **Monitor.** Watch search latency p95, write latency p95 (unchanged — just the enqueue), embedding-queue depth, retry rate, and `procrastinate_jobs.failed` count.

Each step is independently mergeable; steps 1–3 ship as quiet infrastructure with no user-visible effect, step 4 starts populating the column on every write, step 5 is the moment hybrid search goes live for users.
