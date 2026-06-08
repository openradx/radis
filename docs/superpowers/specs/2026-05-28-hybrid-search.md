# Hybrid Search Design (FTS + Dense Vector via Qwen3-Embedding-4B)

**Status:** Draft — design phase
**Author:** RADIS team (Samuel Kwong)
**Date:** 2026-05-28
**Implementation skill (next step):** `writing-plans`
**Supersedes:** `2026-05-15-hybrid-search-design.md`

---

## 1. Overview

RADIS today provides PostgreSQL full-text search (FTS) over radiology reports via the `radis.pgsearch` provider: each `Report` gets a 1:1 `ReportSearchVector` row holding a `tsvector`, kept in sync via `post_save` signal and a bulk re-index task. Queries are ranked by `ts_rank` and snippeted via `ts_headline`.

This spec extends that infrastructure with a dense-vector retrieval side, fused with FTS via Reciprocal Rank Fusion (RRF), to deliver **hybrid search**. Embeddings are produced by a Qwen3-Embedding-4B inference endpoint and stored in the same `ReportSearchVector` table.

The public `SearchProvider` API (`radis.search.site`) is unchanged. Callers — `SearchView`, `ExtractionJob`, `SubscriptionJob`, the REST API — see no signature differences. Only the body of `radis.pgsearch.providers.search()` and `retrieve()` changes.

## 2. Goals & non-goals

### Goals

- Combine the existing FTS recall with semantic recall so queries like "no pneumothorax" surface reports that describe the absence without containing the exact word (modulo the dense-retrieval polarity limitation in §11).
- Keep the existing `SearchProvider` contract intact.
- Index embeddings asynchronously without blocking report ingest.
- Keep embedding load isolated from chat/extraction/subscription LLM tasks.
- Degrade gracefully when the embedding service is unavailable (search continues as FTS-only).
- Make the embedding backend pluggable so Ollama can be used in dev and a Qwen3 endpoint in prod with the same code path.

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
│  2. Vector top-K   ────► ReportSearchVector  (HNSW on .embedding)    │
│                          filtered by structured filters              │
│                                                                      │
│  3. FTS hits       ────► ReportSearchVector  (GIN on .search_vector) │
│                          filtered by structured filters              │
│                                                                      │
│  4. Python-side RRF fusion of (vec_top_K ∪ fts_hits)                 │
│  5. Pagination on the fused order                                    │
│  6. ts_headline() ────► ReportSearchVector  (page-slice only)        │
└──────────────────────────────────────────────────────────────────────┘

┌──────────────────────────────────────────────────────────────────────┐
│  Async indexing path  (inline, per-API-call)                         │
│                                                                      │
│  ADRF view (POST / PUT / bulk-upsert)                                │
│        │                                                             │
│        ▼  database_sync_to_async block                               │
│  ReportSerializer / bulk_upsert_reports                              │
│    ├─ DB write inside transaction.atomic()                           │
│    ├─ FTS path creates ReportSearchVector(embedding=NULL)            │
│    │     (post_save signal for single-create / explicit              │
│    │      bulk_upsert_report_search_vectors for bulk)                │
│    └─ returns the touched report PK(s)                               │
│        │                                                             │
│        ▼  await embed_reports_inline(report_ids)                     │
│  AsyncEmbeddingClient.embed_documents([body, ...])                   │
│    ├─ L2-normalize; ReportSearchVector.objects.bulk_update           │
│    └─ on EmbeddingClientError: log WARNING; embedding stays NULL     │
│                                                                      │
│  Response returned. Report is now hybrid-searchable (vector + FTS).  │
└──────────────────────────────────────────────────────────────────────┘
```

Both ingest paths — single-create (`POST /api/reports/`, `PUT /api/reports/{id}/?upsert=true`) and bulk-upsert (`POST /api/reports/bulk-upsert/`) — embed inline. There is no Procrastinate orchestrator, no periodic launcher, no embeddings worker, no `EmbeddingJob`/`EmbeddingTask` tables. The cost is added latency on the write path (one HTTP round-trip to the embedding service, batched for bulk); the benefit is operational simplicity and immediate searchability.

**Components added inside `radis.pgsearch`:**

| File | Purpose |
|---|---|
| `utils/embedding_client.py` | `EmbeddingClient` (sync, used by the query path) + `AsyncEmbeddingClient` (async, used by the inline view path); pluggable backends (`openai`, `ollama`) |
| `utils/inline_embedding.py` | `embed_reports_inline(report_ids)` — called from the ADRF report views right after a write. Looks up RSVs, calls `AsyncEmbeddingClient.embed_documents`, bulk-updates the embedding column. Catches `EmbeddingClientError` → WARN + leave NULL. |
| `migrations/0002_hybrid_search.py` | Single schema migration: `CREATE EXTENSION vector`; adds `embedding vector(N)` column + HNSW index |
| `models.py` (modified) | Adds `embedding` field + `HnswIndex` to `ReportSearchVector`. No Job/Task models. |
| `signals.py` (unchanged from FTS-only) | The FTS `create_or_update_report_search_vector` receiver stays; **no embedding signal** |
| `tasks.py` (modified) | Keeps `bulk_index_reports` / `enqueue_bulk_index_reports` (existing FTS bulk-indexing). No embedding tasks. |
| `providers.py` (modified) | Replaces `search()` and `retrieve()` bodies with hybrid logic |
| `tests/...` | Coverage per §10 |

**Infrastructure additions:**

| File | Change |
|---|---|
| `pyproject.toml` | Add `pgvector>=0.3` dependency |
| `radis/settings/base.py` | New env-driven + constant settings (§8) |
| `radis/settings/test.py` | Override `EMBEDDING_PROVIDER_URL=""` so the inline path fast-fails into its WARN fallback in CI (no live embedding service) |
| `example.env` | Document `EMBEDDING_*` env vars for openai and ollama backends |
| `radis/reports/api/views.py` (PR #230) | Three ADRF views (`ReportListAPIView` / `ReportDetailAPIView` / `ReportBulkUpsertAPIView`); each `await`s `embed_reports_inline(...)` after the sync write block |

## 4. Schema and migrations

### 4.1 Dependency

Add to `pyproject.toml`:

```toml
"pgvector>=0.3",
```

### 4.2 Schema migration

Schema lives in a single file `radis/pgsearch/migrations/0002_hybrid_search.py`,
depending on `pgsearch.0001_initial` and `reports.0013_alter_report_options`.
Three operations:

1. `RunSQL("CREATE EXTENSION IF NOT EXISTS vector;", reverse_sql=RunSQL.noop)`.
   Reverse is a no-op because the extension may be shared with other Postgres
   usage and dropping it would damage unrelated state. Dev rollback is handled
   by recreating the database.
2. `AddField` `embedding` on `ReportSearchVector`:
   `pgvector.django.vector.VectorField(dimensions=settings.EMBEDDING_DIM, null=True)`.
3. `AddIndex` HNSW on `embedding`: `m=16`, `ef_construction=64`,
   `opclasses=["vector_cosine_ops"]`, `name="pgsearch_embedding_hnsw"`.

The inline-embedding architecture (§6) removes the need for any orchestrator
tables or system user, so this migration carries only schema. Reverse drops
the index and column.

### 4.4 Model update

`radis/pgsearch/models.py`:

```python
from django.conf import settings
from pgvector.django import HnswIndex, VectorField

class ReportSearchVector(models.Model):
    report = models.OneToOneField(Report, on_delete=models.CASCADE, related_name="search_vector")
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
        ]
```

`embedding` is nullable: the row exists from the moment a `Report` is created (FTS path), but its embedding is filled inline by the ADRF view's `await embed_reports_inline(...)` step (§6). A NULL embedding is treated as "not embedded yet" at query time, and the row participates via the FTS half only.

`save()` on `ReportSearchVector` retains its current behavior of recomputing `search_vector` from `report.body`. The embedding column is written **only** by `embed_reports_inline` via `bulk_update()`, never by `save()`, to avoid triggering the FTS signal recursively and to keep the two indexing paths independent.

### 4.5 Operational note on `EMBEDDING_DIM`

pgvector columns and HNSW indexes are bound to a fixed dimension at create time, and HNSW has a 2000-dim ceiling (so `EMBEDDING_DIM ≤ 2000`; Qwen3-Embedding-4B's native 2560 is Matryoshka-truncated client-side). Changing `EMBEDDING_DIM` after deploy requires a manual operator procedure:

1. Drop the HNSW index and the `embedding` column.
2. Re-run `0002_hybrid_search` with the new `EMBEDDING_DIM`. This re-creates
   the column at the new dim plus the HNSW index.
3. New report writes start populating the column automatically (the ADRF
   views embed inline on every POST/PUT/bulk-upsert — §6). For historical
   reports whose `embedding` is now NULL, the operator re-PUTs each affected
   document (idempotent — same body, new vector). No backfill command is
   provided.

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
    """Return the `dimensions` value of `ReportSearchVector.embedding` as
    captured by the on-disk pgsearch migrations. Returns None if the field
    cannot be located (e.g., migrations are missing or out of sync)."""
    from django.db.migrations.loader import MigrationLoader

    loader = MigrationLoader(connection=None, ignore_no_migrations=True)
    state = loader.project_state()
    try:
        model = state.apps.get_model("pgsearch", "ReportSearchVector")
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
                 "that adds `embedding` to `ReportSearchVector`.",
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

- `class EmbeddingBackend(Protocol)` with `path`, `build_payload`, `parse_response`.
- `class OpenAIBackend(EmbeddingBackend)` — default path `/v1/embeddings`, body `{model, input: [...]}`, response `{data: [{embedding: [...]}]}`.
- `class OllamaBackend(EmbeddingBackend)` — default path `/api/embed`, body `{model, input: [...]}`, response `{embeddings: [[...]]}`.
- `BACKENDS: dict[str, EmbeddingBackend] = {"openai": OpenAIBackend(), "ollama": OllamaBackend()}`.
- `class EmbeddingClientError(Exception)`.
- `class EmbeddingClient` — sync client used by the query path (`providers.search` / `providers.retrieve`).
- `class AsyncEmbeddingClient` — async sibling of `EmbeddingClient`, used by `embed_reports_inline` from the ADRF report views (§6). Same backend protocol; differs only in using `httpx.AsyncClient` + an `async with` lifecycle.

### 5.2 Interface

```python
class EmbeddingClient:
    def __init__(self):
        self._backend = BACKENDS[settings.EMBEDDING_BACKEND]
        self._path = settings.EMBEDDING_PROVIDER_PATH or self._backend.path
        self._url = settings.EMBEDDING_PROVIDER_URL.rstrip("/") + self._path
        self._model = settings.EMBEDDING_MODEL_NAME
        self._timeout = settings.EMBEDDING_REQUEST_TIMEOUT
        self._headers = {"Authorization": f"Bearer {settings.EMBEDDING_PROVIDER_API_KEY}"} \
                        if settings.EMBEDDING_PROVIDER_API_KEY else {}

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """Embed texts verbatim. Truncates each to EMBEDDING_MAX_INPUT_CHARS first.
        Returns L2-normalized vectors of length EMBEDDING_DIM."""

    def embed_query(self, text: str) -> list[float]:
        """Prepend EMBEDDING_QUERY_INSTRUCTION, then embed_documents([text])[0]."""
```

### 5.3 Wire shapes

| Backend | Path (default) | Request | Response |
|---|---|---|---|
| `openai` | `/v1/embeddings` | `{"model": M, "input": [t, ...]}` | `{"data": [{"embedding": [...]}, ...]}` |
| `ollama` | `/api/embed` | `{"model": M, "input": [t, ...]}` | `{"embeddings": [[...], ...]}` |

`EMBEDDING_PROVIDER_PATH` (env) overrides the backend default — this is how the production endpoint at `/api/embeddings` with an OpenAI-style payload is supported by the `openai` backend with a one-line config change, no new backend needed.

### 5.4 Behavior details

- **Query instruction:** the model card for Qwen3-Embedding recommends a task-specific instruction prefix on the query side only. `embed_query` prepends `EMBEDDING_QUERY_INSTRUCTION` (a Python constant in `base.py`); `embed_documents` does not.
- **Truncation:** any text longer than `EMBEDDING_MAX_INPUT_CHARS` is truncated at the character limit before being sent. A WARNING is logged with the report id (when known) and char count. Qwen3-Embedding-4B supports up to 32k tokens, so truncation will be rare for radiology bodies but is bounded as a defense against pathological inputs.
- **Normalization:** every returned vector is L2-normalized client-side, unconditionally. With unit vectors, cosine distance is monotonic in dot product, which makes the HNSW `vector_cosine_ops` operator effectively a fast inner-product search. Whether the upstream server normalizes is irrelevant.
- **Dimension validation:** every vector is checked to have length `EMBEDDING_DIM`. A mismatch raises `EmbeddingClientError`.
- **Batching:** `embed_documents` sends a single HTTP call per invocation. For the inline write path (§6), the ADRF bulk-upsert view passes the entire touched-report set in one call; the embedding service is responsible for handling the batch size it advertises. For the optional sync re-embed of a historical corpus, the operator re-PUTs reports through the API one (or many) at a time.
- **Errors:** non-2xx, timeout, malformed JSON, missing key, or wrong dim all raise `EmbeddingClientError`. The client never falls back internally — fallback policy is owned by the caller.
- **Dev recipe (Ollama):**
  ```bash
  ollama pull dengcao/Qwen3-Embedding-4B:Q5_K_M
  # in .env:
  EMBEDDING_BACKEND=ollama
  EMBEDDING_PROVIDER_URL=http://host.docker.internal:11434
  EMBEDDING_MODEL_NAME=dengcao/Qwen3-Embedding-4B:Q5_K_M
  EMBEDDING_DIM=2560
  ```
  GGUF-quantized embedding models produce slightly different vectors than the bf16 reference, so dev embeddings are not interchangeable with prod embeddings. After swapping the model between dev/prod, follow the §4.5 dim-change procedure (drop column, re-migrate, re-PUT historical reports).

## 6. Async indexing (inline, per-API-call)

Embedding happens **inline inside the async ADRF report views** (PR #230), not on a background queue. When a `POST /api/reports/`, `PUT /api/reports/{id}/?upsert=true`, or `POST /api/reports/bulk-upsert/` request arrives, the view does its sync write inside a `database_sync_to_async` block, then `await`s `embed_reports_inline(report_ids)` before responding. No `EmbeddingJob`/`EmbeddingTask`, no `embedding_launcher`, no `embeddings_worker` container.

### 6.1 The helper

`radis/pgsearch/utils/inline_embedding.py`:

```python
async def embed_reports_inline(report_ids: list[int]) -> None:
    """Compute embeddings for `report_ids` and write to ReportSearchVector.

    On embedding-service failure: log WARNING, return; RSV stays NULL and the
    report remains searchable via the FTS half. Never raises.
    """
    if not report_ids:
        return

    @database_sync_to_async
    def _load_rsvs() -> list[ReportSearchVector]:
        return list(
            ReportSearchVector.objects.filter(report_id__in=report_ids)
            .select_related("report").only("id", "report_id", "report__body")
        )

    rsvs = await _load_rsvs()
    if not rsvs:
        return

    try:
        async with AsyncEmbeddingClient() as client:
            vectors = await client.embed_documents([rsv.report.body for rsv in rsvs])
    except EmbeddingClientError as exc:
        logger.warning("Inline embedding failed for %d report(s): %s", len(rsvs), exc)
        return

    for rsv, vec in zip(rsvs, vectors, strict=True):
        rsv.embedding = vec

    @database_sync_to_async
    def _save_embeddings() -> None:
        ReportSearchVector.objects.bulk_update(rsvs, fields=["embedding"])

    await _save_embeddings()
```

### 6.2 View wiring

Each ADRF view returns the touched report PKs from its sync block, then awaits the helper:

- `ReportListAPIView.post` — `_create()` returns `(serializer.data, report.pk)`; view then `await embed_reports_inline([report_pk])`.
- `ReportDetailAPIView.put` — `_save()` returns `(serializer.data, http_status, saved.pk)`; view then `await embed_reports_inline([saved_pk])`. Both update and upsert paths embed (a `PUT` may have changed `body`).
- `ReportBulkUpsertAPIView.post` — `_do()` calls `bulk_upsert_reports(valid_payloads)`, then looks up touched PKs via `Report.objects.filter(document_id__in=...).values_list("pk", flat=True)`, and returns `(body, touched_report_pks)`. View then `await embed_reports_inline(touched_report_pks)`. The embedding HTTP call is a single fan-out: `embed_documents([body, ...])` over the full touched set.

### 6.3 AsyncEmbeddingClient

`radis/pgsearch/utils/embedding_client.py` defines two clients with shared internals:

- `EmbeddingClient` (sync, `httpx.Client`) — used by the **query path** (`providers.search`), still synchronous because the search provider interface is sync.
- `AsyncEmbeddingClient` (async, `httpx.AsyncClient`) — used by the **write path** via `embed_reports_inline`. Supports `async with` lifecycle, `await embed_documents(...)`, `await embed_query(...)`.

Both pull from a shared `_resolve_config()` (validation of `EMBEDDING_*` settings) and a shared `_normalize_response()` (L2-normalize, dim check, Matryoshka truncation). The only divergence is the HTTP call.

### 6.4 Failure semantics

| Failure | Effect on the request |
|---|---|
| Embedding service unreachable / 5xx / timeout | `embed_reports_inline` catches `EmbeddingClientError`, logs WARNING, returns. RSV stays NULL. **API request succeeds.** Hybrid search degrades to FTS-only for that report until it's re-PUT or the operator re-embeds. |
| `EMBEDDING_PROVIDER_URL` empty (e.g. test settings) | Same. Client `__init__` raises `EmbeddingClientError`, helper catches it. |
| Backend returns wrong-dim vector / malformed body | Same `EmbeddingClientError` path. |

The write path never fails because of embedding. Reports are always saved.

### 6.5 What about historical NULLs?

No backfill command. Reports loaded before the inline path was in place keep `embedding=NULL` until re-PUT. They remain hybrid-searchable via the FTS half. Operators who need vector coverage on a historical corpus re-upload each affected document; the inline path embeds it on the way through.

This is a deliberate trade-off: the orchestrator-and-launcher infrastructure that used to handle "backfill the existing corpus" was substantial (≈400 lines plus a dedicated worker), and the inline path makes new writes immediately hybrid-searchable, which is what the architecture optimises for. A one-time backfill is a one-time concern.

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
def search(s: Search) -> SearchResult:
    query_str = _build_query_string(s.query)
    language  = _resolve_language(s.filters)
    filter_q  = _build_filter_query(s.filters)
    tsquery   = SearchQuery(query_str, search_type="raw", config=language)

    # Vector side: strip NOT branches before embedding (see §7.8), then embed.
    # If stripping leaves nothing (e.g., the user query was just `NOT X`),
    # skip vector retrieval entirely and fall through to FTS-only.
    query_text = QueryParser.unparse_for_embedding(s.query)
    query_vec: list[float] | None = None
    if query_text.strip():
        try:
            query_vec = EmbeddingClient().embed_query(query_text)
        except EmbeddingClientError as e:
            logger.warning("Falling back to FTS-only: %s", e)
            query_vec = None

    vec_rank: dict[int, int] = {}
    if query_vec is not None:
        ids = list(
            ReportSearchVector.objects
                .filter(filter_q)
                .exclude(embedding__isnull=True)
                .annotate(distance=CosineDistance("embedding", query_vec))
                .order_by("distance", "report_id")
                .values_list("report_id", flat=True)[:settings.HYBRID_VECTOR_TOP_K]
        )
        vec_rank = {rid: i + 1 for i, rid in enumerate(ids)}

    # FTS side
    fts_rows = list(
        ReportSearchVector.objects
            .filter(filter_q)
            .filter(search_vector=tsquery)
            .annotate(rank=SearchRank(F("search_vector"), tsquery))
            .order_by("-rank", "report_id")
            .values("report_id", "rank")[:settings.HYBRID_FTS_MAX_RESULTS]
    )
    fts_rank = {row["report_id"]: i + 1 for i, row in enumerate(fts_rows)}

    # Fusion (pure Python, factored out for unit testing)
    ordered_ids = _rrf_fuse(vec_rank, fts_rank, k=settings.HYBRID_RRF_K)

    total_count = len(ordered_ids)
    total_relation = (
        "at_least"
        if len(fts_rows) >= settings.HYBRID_FTS_MAX_RESULTS
           or len(vec_rank) >= settings.HYBRID_VECTOR_TOP_K
        else "exact"
    )
    page_ids = ordered_ids[s.offset : s.offset + (s.limit or len(ordered_ids))]

    # Headline + hydration for the page slice only
    page_rows = (
        ReportSearchVector.objects
            .filter(report_id__in=page_ids)
            .annotate(
                summary=SearchHeadline("report__body", tsquery, config=language,
                                       start_sel="<em>", stop_sel="</em>",
                                       min_words=10, max_words=20, max_fragments=10),
                rank=SearchRank(F("search_vector"), tsquery),
            )
            .select_related("report")
    )
    by_id = {r.report_id: r for r in page_rows}
    documents = [
        document_from_pgsearch_response(_with_fallback_summary(by_id[rid]))
        for rid in page_ids if rid in by_id
    ]
    return SearchResult(total_count=total_count, total_relation=total_relation, documents=documents)
```

### 7.3 Empty-summary fallback

`SearchHeadline` returns an empty string when the document body has no FTS match (the vector-only hit case). `_with_fallback_summary` replaces an empty summary with the first 30 words of `report.body`. Trivial helper, ~5 lines.

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
EMBEDDING_BACKEND          = env.str("EMBEDDING_BACKEND", default="openai")
EMBEDDING_PROVIDER_URL     = env.str("EMBEDDING_PROVIDER_URL", default="")
EMBEDDING_PROVIDER_PATH    = env.str("EMBEDDING_PROVIDER_PATH", default="")   # "" = backend default
EMBEDDING_PROVIDER_API_KEY = env.str("EMBEDDING_PROVIDER_API_KEY", default="")
EMBEDDING_MODEL_NAME       = env.str("EMBEDDING_MODEL_NAME", default="Qwen/Qwen3-Embedding-4B")
EMBEDDING_DIM              = env.int("EMBEDDING_DIM", default=1024)
```

These vary across dev/staging/prod and are operator-controlled. `EMBEDDING_DIM` is intentionally an env decision because it is schema-coupled (see §4.5).

### 8.2 Code constants (tuning knobs, in `base.py`)

```python
EMBEDDING_REQUEST_TIMEOUT = 30  # seconds
EMBEDDING_MAX_INPUT_CHARS = 60_000
EMBEDDING_QUERY_INSTRUCTION = (
    "Instruct: Given a radiology search query, retrieve relevant radiology reports.\n"
    "Query: "
)
EMBEDDING_BATCH_SIZE = 32

HYBRID_VECTOR_TOP_K    = 100
HYBRID_FTS_MAX_RESULTS = 10_000
HYBRID_RRF_K           = 60
```

These are tuning constants. Changing them is a code change with a PR diff. This matches the project's existing pattern (`EXTRACTION_LLM_CONCURRENCY_LIMIT = 6`, the `CHAT_*_SYSTEM_PROMPT` blocks).

### 8.3 `example.env`

Adds a documented Ollama block and a Qwen/OpenAI-compatible block side by side, keyed off `EMBEDDING_BACKEND`.

### 8.4 Compose

`docker-compose.base.yml`:

- The `EMBEDDING_BACKEND`, `EMBEDDING_PROVIDER_URL`, `EMBEDDING_PROVIDER_PATH`, `EMBEDDING_PROVIDER_API_KEY`, `EMBEDDING_MODEL_NAME`, `EMBEDDING_DIM` env keys are added to the `&default-app` block so all services see them. No dedicated `embeddings_worker` container — embedding runs inside the `web` ASGI service.

`docker-compose.dev.yml`:

No `embeddings_worker` block in either file — the inline architecture (§6) embeds inside the request, so the `web` service handles it directly.

## 9. Error handling and degradation

| Failure | Behavior | Logging |
|---|---|---|
| Embedding service returns 5xx/timeout during query-time | `query_vec = None`; result list ordered by FTS-only; request succeeds | WARNING with request id |
| Embedding service returns 4xx during query-time | Same FTS-only fallback (treats as misconfig at request layer) | ERROR |
| Embedding service returns malformed body | `EmbeddingClientError` raised; query falls back to FTS-only | ERROR |
| Embedding service down during inline-embedding call | `embed_reports_inline` catches `EmbeddingClientError`; `embedding` stays NULL; **API request still succeeds** | WARNING with report count |
| Orchestrator crashes during task creation (partial dispatch) | Job stays in `PREPARING`. Next launcher tick sees in-flight job and no-ops. Operator marks job `FAILURE` in admin to allow a fresh run | ERROR + operator action |
| Sub-task fails after Procrastinate retries exhausted | Task ends as `FAILURE`. `update_job_state` rolls the job to `WARNING` (some tasks succeeded) or `FAILURE` (all failed). NULL rows remain; next launcher creates a new job to retry them | ERROR |
| Report body > `EMBEDDING_MAX_INPUT_CHARS` | Truncate, embed truncated text | WARNING with report_id and char count |
| Report deleted between task creation and execution | Sub-task's `task.reports.values_list(...)` returns fewer rows; `embed_documents` called on smaller list; no error | DEBUG |
| Vector dim mismatch on write | Postgres raises; sub-task fails, retried | ERROR — escalate to admin |
| `EMBEDDING_PROVIDER_URL` empty at startup | `EmbeddingClient` construction defers to call site; calls log + raise; query falls back to FTS-only | WARNING once on first request |
| System user missing (data migration didn't run) | Launcher raises `User.DoesNotExist`. Loud failure; deployment misconfiguration. Fix: run migrations | ERROR |

**Deliberate non-policies:**

- The product never fails a search request because the embedding service is down. It degrades to FTS-only.
- Query embeddings are not cached. The complexity and freshness trade-off is not worth it at the corpora sizes RADIS targets.
- `EmbeddingClient` does not retry internally. Procrastinate retries the whole task; the query path uses a single shot.

**Observability:**

- Provider logs at DEBUG: vec hit count, FTS hit count, intersection count, fusion ms, query-embed ms.
- `embed_reports_inline` logs at INFO: batch size, total chars, latency.
- The existing OpenTelemetry overlay (commit `653e0c67`) tags telemetry per service; the inline embedding span shows up under the `web` ASGI service.

## 10. Testing strategy

### 10.1 Unit tests (no DB)

| File | Coverage |
|---|---|
| `tests/unit/test_embedding_client.py` | Backend payload/response round-trip, path override, instruction prefix, normalization, dim validation, all error modes, truncation |
| `tests/unit/test_provider_fusion.py` | `_rrf_fuse(vec_rank, fts_rank, k)` pure-Python helper: disjoint, overlapping, FTS-only, vector-only, both-empty, tiebreak by report_id |
| `tests/unit/test_inline_embedding.py` | Loads RSVs by report_id, calls `AsyncEmbeddingClient.embed_documents`, bulk-updates vectors. Asserts the catch-and-WARN path on `EmbeddingClientError` (RSV stays NULL, never raises). |

### 10.2 Integration tests (real Postgres + pgvector)

| File | Coverage |
|---|---|
| `tests/integration/test_migrations.py` (new, `django-test-migrations`) | Extension migration runs; column + HNSW index created with configured dim; reverse works |
| `tests/integration/test_provider_hybrid.py` (new) | FTS-only hit, vector-only hit ("no pneumothorax" fixture), both-sides hit, filter honoring, stable pagination, embedding-service-down fallback, NULL-embedding rows still returned, `ts_headline` query-count bounded to page, empty-summary fallback |

Factories: existing `ReportSearchVectorFactory` gains optional `embedding` kwarg (default `None`). New `ReportSearchVectorWithEmbeddingFactory` generates deterministic normalized vectors of the configured dim from a seed. Real Qwen3 embeddings are not used in tests.

### 10.3 View-level smoke

`radis/search/tests/test_views.py` (extend):

- Search request with hybrid enabled returns 200 and renders documents.
- Search request with `EMBEDDING_PROVIDER_URL=""` returns 200 (FTS-only path).

### 10.4 Acceptance (`@pytest.mark.acceptance`)

One end-to-end test against the dev containers, with the embedding service stubbed (either a small in-test FastAPI or a recorded fixture response), verifying the search page returns hybrid results. Marked acceptance so it's opt-in like the existing acceptance suite.

### 10.5 Explicitly not tested

- Live Qwen3 retrieval quality (offline eval, out of scope).
- pgvector HNSW recall under specific data shapes (extension's responsibility).
- Wire formats beyond the two supported backends.

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

Documented in §5.4. Mitigated by following §4.5 after a model swap: drop the column, re-migrate, re-PUT historical reports through the ADRF API to embed them with the new model.

### 11.4 No body-change detection for re-embedding

V1 re-embeds anything where `embedding IS NULL`. A future optimization could
track whether the body actually changed (e.g., a `body_hash` column on
`ReportSearchVector` updated only on body changes) so metadata-only updates
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

1. **Schema + dep.** `pgvector` pip dep + squashed `0002_hybrid_search` migration (extension + embedding column + HNSW). No behaviour change yet.
2. **Embedding clients + inline helper + tests.** Land `EmbeddingClient`, `AsyncEmbeddingClient`, and `embed_reports_inline`. No callers wired up yet.
3. **ADRF views wiring (depends on PR #230).** Once PR #230 lands, modify the three ADRF views to `await embed_reports_inline(...)` after their sync write blocks. From this point on, **every report write embeds inline**.
4. **Provider switch.** Replace the body of `radis.pgsearch.providers.search()` and `retrieve()` with the hybrid implementation. Rows still missing an embedding participate via the FTS half only.
5. **(Optional) historical backfill.** Operators re-PUT each previously-loaded document to trigger an embedding. No dedicated tool; the ingest API is the embedding tool.
6. **Monitor.** Watch search latency p95, write latency p95 (now includes embedding), and the rate of FTS-only-fallback WARNING logs. Tune `HYBRID_VECTOR_TOP_K` / `HYBRID_FTS_MAX_RESULTS` if needed.

Each step is independently mergeable; steps 1–2 ship as quiet infrastructure with no user-visible effect, step 3 starts populating the column on every write, step 4 is the moment hybrid search goes live for users.
