# Hybrid Search Design (FTS + Dense Vector via Qwen3-Embedding-4B)

**Status:** Draft — design phase
**Author:** RADIS team (Samuel Kwong)
**Date:** 2026-05-15
**Implementation skill (next step):** `writing-plans`

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
- No automated re-embedding when `EMBEDDING_DIM` changes. That is a manual operator procedure: drop column, re-migrate, run `backfill_embeddings`.
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
│  Async indexing path                                                 │
│                                                                      │
│  Report.save() ──post_save──► enqueue_embed_reports([id])            │
│                                  │                                   │
│                                  ▼                                   │
│                       Procrastinate queue: "embeddings"              │
│                                  │                                   │
│                                  ▼                                   │
│  embeddings_worker ──► embed_reports(ids)                            │
│                          ├─ EmbeddingClient.embed_documents(...)     │
│                          ├─ L2-normalize                             │
│                          └─ ReportSearchVector.objects.update()      │
│                                                                      │
│  ./manage.py backfill_embeddings ──► batched enqueue on same queue   │
└──────────────────────────────────────────────────────────────────────┘
```

**Components added inside `radis.pgsearch`:**

| File | Purpose |
|---|---|
| `utils/embedding_client.py` | Sync + async HTTP clients with pluggable backends (`openai`, `ollama`) |
| `migrations/0002_pgvector_extension.py` | `CREATE EXTENSION IF NOT EXISTS vector;` |
| `migrations/0003_report_embedding.py` | Adds `embedding vector(N)` column + HNSW index |
| `models.py` (modified) | Adds `embedding` field + `HnswIndex` |
| `signals.py` (modified) | Adds second `post_save` receiver to enqueue embedding |
| `tasks.py` (modified) | Adds `embed_reports` Procrastinate task on `embeddings` queue |
| `providers.py` (modified) | Replaces `search()` and `retrieve()` bodies with hybrid logic |
| `management/commands/backfill_embeddings.py` | Idempotent backfill command |
| `tests/...` | Coverage per §10 |

**Infrastructure additions:**

| File | Change |
|---|---|
| `pyproject.toml` | Add `pgvector>=0.3` dependency |
| `radis/settings/base.py` | New env-driven + constant settings (§8) |
| `example.env` | Document `EMBEDDING_*` env vars for openai and ollama backends |
| `docker-compose.base.yml` | Add `embeddings_worker` service + `EMBEDDING_*` env vars |
| `docker-compose.dev.yml` / `.prod.yml` | `embeddings_worker.command` running `bg_worker -q embeddings` |

## 4. Schema and migrations

### 4.1 Dependency

Add to `pyproject.toml`:

```toml
"pgvector>=0.3",
```

### 4.2 Postgres extension migration

`radis/pgsearch/migrations/0002_pgvector_extension.py`:

```python
class Migration(migrations.Migration):
    dependencies = [("pgsearch", "0001_initial")]
    operations = [
        migrations.RunSQL(
            sql="CREATE EXTENSION IF NOT EXISTS vector;",
            reverse_sql=migrations.RunSQL.noop,   # do not drop in prod
        ),
    ]
```

Reverse is a no-op because the extension may be shared with other Postgres usage and dropping it would damage unrelated state. Dev rollback is handled by recreating the database.

### 4.3 Schema migration

`radis/pgsearch/migrations/0003_report_embedding.py`: standard `AddField` with a `VectorField(dimensions=settings.EMBEDDING_DIM, null=True)` and `AddIndex` for an `HnswIndex` with `opclasses=["vector_cosine_ops"]`, `m=16`, `ef_construction=64`.

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

`embedding` is nullable: the row exists from the moment a `Report` is created (FTS path), but its embedding is filled asynchronously by `embed_reports`. A NULL embedding is treated as "not embedded yet" at query time, and the row participates via the FTS half only.

`save()` on `ReportSearchVector` retains its current behavior of recomputing `search_vector` from `report.body`. The embedding column is written **only** by the embedding task via `update()`, never by `save()`, to avoid triggering the FTS signal recursively and to keep the two indexing paths independent.

### 4.5 Operational note on `EMBEDDING_DIM`

pgvector columns and HNSW indexes are bound to a fixed dimension at create time. Changing `EMBEDDING_DIM` after deploy requires a manual operator procedure:

1. Drop the HNSW index and the `embedding` column.
2. Re-run `0003_report_embedding` with the new `EMBEDDING_DIM`.
3. Run `./manage.py backfill_embeddings`.

This is documented as a deployment-time decision and intentionally not automated.

## 5. Embedding client

### 5.1 Module layout

`radis/pgsearch/utils/embedding_client.py` exposes:

- `class EmbeddingBackend(Protocol)` with `path`, `build_payload`, `parse_response`.
- `class OpenAIBackend(EmbeddingBackend)` — default path `/v1/embeddings`, body `{model, input: [...]}`, response `{data: [{embedding: [...]}]}`.
- `class OllamaBackend(EmbeddingBackend)` — default path `/api/embed`, body `{model, input: [...]}`, response `{embeddings: [[...]]}`.
- `BACKENDS: dict[str, EmbeddingBackend] = {"openai": OpenAIBackend(), "ollama": OllamaBackend()}`.
- `class EmbeddingClientError(Exception)`.
- `class EmbeddingClient` — sync client used by `embed_reports` task and the query path.
- `class AsyncEmbeddingClient` — async variant, kept for parity with `chats/utils/chat_client.py` and so the query path can call it from ASGI views without `async_to_sync` later.

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
- **Batching:** `embed_documents` sends a single HTTP call per invocation. Higher-level callers (`embed_reports` task) split into batches of `EMBEDDING_BATCH_SIZE` before calling.
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
  GGUF-quantized embedding models produce slightly different vectors than the bf16 reference, so dev embeddings are not interchangeable with prod embeddings. After swapping the model between dev/prod, run `backfill_embeddings`.

## 6. Async indexing

### 6.1 Queue and worker

A new Procrastinate queue named **`embeddings`** is added, served by a new container **`embeddings_worker`**. This isolates embedding load from the existing `default` and `llm` queues. The `embeddings` worker's command:

```
./manage.py bg_worker -l debug -q embeddings --autoreload   # dev
./manage.py bg_worker -l info  -q embeddings                # prod
```

The worker inherits the same image and environment as `default_worker` / `llm_worker` via the existing `&default-app` anchor.

### 6.2 Priorities

Procrastinate priority is "higher = sooner". Embedding tasks always run at lower priority than the existing LLM tasks so a backfill never starves extraction/subscription work — though in practice this only matters *within* a queue, and `embeddings` is a separate queue from `llm`. The priorities are still set defensively in case workers are ever consolidated:

| Task | Priority |
|---|---|
| `EXTRACTION_DEFAULT_PRIORITY` (existing) | 2 |
| `EXTRACTION_URGENT_PRIORITY` (existing) | 3 |
| `SUBSCRIPTION_DEFAULT_PRIORITY` (existing) | 3 |
| `SUBSCRIPTION_URGENT_PRIORITY` (existing) | 4 |
| `EMBEDDING_INDEX_PRIORITY` (new) | 0 |
| `EMBEDDING_BACKFILL_PRIORITY` (new) | -1 |

Backfill below incremental ensures fresh-report embeddings always overtake a backfill job in flight.

### 6.3 Task: `embed_reports`

`radis/pgsearch/tasks.py`:

```python
@app.task(queue="embeddings")
def embed_reports(report_ids: list[int]) -> None:
    """Embed the given reports and write the vector to ReportSearchVector.embedding.
    Idempotent. Skips rows that already have an embedding."""
```

Implementation outline:

1. `target = ReportSearchVector.objects.filter(report_id__in=ids).select_related("report").only("report_id", "report__body")`. No `embedding__isnull` short-circuit at this layer — the task always re-embeds whatever it is given. Backfill controls the "only fill in nulls" policy by filtering at enqueue time (§6.5).
2. Iterate in chunks of `EMBEDDING_BATCH_SIZE`; for each chunk, call `EmbeddingClient().embed_documents([rsv.report.body for rsv in chunk])`.
3. `ReportSearchVector.objects.filter(pk=rsv.pk).update(embedding=vec)` per row. (Postgres `UPDATE … SET embedding = CASE pk WHEN … END` is a possible optimization if profiling shows the per-row update is a bottleneck; not done in v1.)
4. Any `EmbeddingClientError` is re-raised so Procrastinate's default retry policy with exponential backoff handles transient failures.

Helper `enqueue_embed_reports(report_ids, priority=settings.EMBEDDING_INDEX_PRIORITY)` mirrors the existing `enqueue_bulk_index_reports`.

**V1 re-embedding policy:** the signal enqueues on every `Report.save()`, including metadata-only updates, so metadata edits trigger a wasted re-embed. Accepted simplicity for v1; §11.4 documents body-change detection as a future optimization.

### 6.4 Signal

`radis/pgsearch/signals.py` keeps the existing receiver for the FTS path and adds:

```python
@receiver(post_save, sender=Report)
def enqueue_report_embedding(sender, instance, **kwargs):
    enqueue_embed_reports([instance.pk], priority=settings.EMBEDDING_INDEX_PRIORITY)
```

Two separate receivers (not one combined) so an enqueue error in the embedding path cannot break the FTS-indexing path. The signal fires on both create and update; `embed_reports` always overwrites the embedding for the given ids, so metadata-only updates do trigger an unnecessary re-embed in v1. Body-change detection (a `pre_save` that suppresses enqueue when only metadata changed) is an optimization deferred to §11.4. `ReportSearchVector.save()` is *not* modified to null `embedding` — the task's unconditional overwrite makes that redundant.

### 6.5 Backfill command

`radis/pgsearch/management/commands/backfill_embeddings.py`:

```
./manage.py backfill_embeddings [--batch-size 500] [--limit N] [--dry-run]
```

Behavior:

- Iterates `ReportSearchVector.objects.filter(embedding__isnull=True).values_list("report_id", flat=True)`.
- Chunks ids by `--batch-size` (default 500).
- For each chunk, calls `enqueue_embed_reports(chunk, priority=settings.EMBEDDING_BACKFILL_PRIORITY)`.
- `--limit N` caps total reports enqueued.
- `--dry-run` skips enqueue and prints the would-be count.
- The "only fill in nulls" filter is applied at enqueue time (here), not inside the task. Re-running the command is safe because rows that got embedded since the last run no longer match the `embedding__isnull=True` filter and won't be re-enqueued.

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

    # Vector side
    query_text = QueryParser.unparse(s.query)  # same helper SearchView already uses
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

### 7.6 `ReportDocument.relevance`

Kept as `ts_rank` for API backwards compatibility. RRF is an internal ordering signal and is not exposed on the public document type. RRF scores are logged at DEBUG for diagnostics.

### 7.7 `search_provider.max_results`

Updated to `max(HYBRID_VECTOR_TOP_K, HYBRID_FTS_MAX_RESULTS)`, which is what the `SearchView` page-bound check uses to reject impossibly-deep pagination.

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

EMBEDDING_INDEX_PRIORITY = 0
EMBEDDING_BACKFILL_PRIORITY = -1

HYBRID_VECTOR_TOP_K    = 100
HYBRID_FTS_MAX_RESULTS = 10_000
HYBRID_RRF_K           = 60
```

These are tuning constants. Changing them is a code change with a PR diff. This matches the project's existing pattern (`EXTRACTION_LLM_CONCURRENCY_LIMIT = 6`, the `CHAT_*_SYSTEM_PROMPT` blocks).

### 8.3 `example.env`

Adds a documented Ollama block and a Qwen/OpenAI-compatible block side by side, keyed off `EMBEDDING_BACKEND`.

### 8.4 Compose

`docker-compose.base.yml`:

- New service `embeddings_worker` inheriting `*default-app`.
- The `EMBEDDING_BACKEND`, `EMBEDDING_PROVIDER_URL`, `EMBEDDING_PROVIDER_PATH`, `EMBEDDING_PROVIDER_API_KEY`, `EMBEDDING_MODEL_NAME`, `EMBEDDING_DIM` env keys added to the `&default-app` block so all services see them.

`docker-compose.dev.yml`:

- `embeddings_worker.command`: `bash -c "wait-for-it -s postgres.local:5432 -t ${WAIT_POSTGRES_TIMEOUT:-180} && ./manage.py bg_worker -l debug -q embeddings --autoreload"`.

`docker-compose.prod.yml`:

- Same without `--autoreload`, log level `info`.

## 9. Error handling and degradation

| Failure | Behavior | Logging |
|---|---|---|
| Embedding service returns 5xx/timeout during query-time | `query_vec = None`; result list ordered by FTS-only; request succeeds | WARNING with request id |
| Embedding service returns 4xx during query-time | Same FTS-only fallback (treats as misconfig at request layer) | ERROR |
| Embedding service returns malformed body | `EmbeddingClientError` raised; query falls back to FTS-only | ERROR |
| Embedding service down during indexing task | Task raises; Procrastinate retries with exponential backoff; `embedding` stays NULL | WARNING per attempt, ERROR after final retry |
| Report body > `EMBEDDING_MAX_INPUT_CHARS` | Truncate, embed truncated text | WARNING with report_id and char count |
| Report deleted between enqueue and task run | Task fetches no rows for that id; no error | DEBUG |
| Vector dim mismatch on write | Postgres raises; task fails, retried | ERROR — escalate to admin |
| `EMBEDDING_PROVIDER_URL` empty at startup | `EmbeddingClient` construction defers to call site; calls log + raise; query falls back to FTS-only | WARNING once on first request |

**Deliberate non-policies:**

- The product never fails a search request because the embedding service is down. It degrades to FTS-only.
- Query embeddings are not cached. The complexity and freshness trade-off is not worth it at the corpora sizes RADIS targets.
- `EmbeddingClient` does not retry internally. Procrastinate retries the whole task; the query path uses a single shot.

**Observability:**

- Provider logs at DEBUG: vec hit count, FTS hit count, intersection count, fusion ms, query-embed ms.
- `embed_reports` logs at INFO: batch size, total chars, latency, success/skip/retry counts.
- The existing OpenTelemetry overlay (commit `653e0c67`) tags telemetry per service; `embeddings_worker` shows up automatically.

## 10. Testing strategy

### 10.1 Unit tests (no DB)

| File | Coverage |
|---|---|
| `tests/unit/test_embedding_client.py` | Backend payload/response round-trip, path override, instruction prefix, normalization, dim validation, all error modes, truncation |
| `tests/unit/test_provider_fusion.py` | `_rrf_fuse(vec_rank, fts_rank, k)` pure-Python helper: disjoint, overlapping, FTS-only, vector-only, both-empty, tiebreak by report_id |
| `tests/unit/test_signals.py` | `post_save` enqueues `embed_reports([id])` with `EMBEDDING_INDEX_PRIORITY` |
| `tests/unit/test_tasks.py` (extends existing) | Always overwrites embedding when re-run (no internal short-circuit); batch splitting; missing ids are skipped without error; client errors propagate so Procrastinate retries |
| `tests/unit/test_backfill_command.py` | Batching, `--limit`, `--dry-run`, only-null-embedding selection |

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

Documented in §5.4. Mitigated by running `backfill_embeddings` after a model swap.

### 11.4 No body-change detection in the signal

V1 re-embeds on every `Report.save()`. If profiling shows wasted traffic from metadata-only updates, add a `pre_save` that only nulls `embedding` when `body` changed.

### 11.5 Per-row `UPDATE` in the embedding task

V1 issues one `UPDATE` per row inside a batch. If this becomes a bottleneck, switch to a single `UPDATE … FROM (VALUES …)` or pgvector's `bulk_create` with `update_conflicts`.

## 12. Rollout plan

1. **Schema and dependency.** Land the `pgvector` Python dep, the extension migration, and the schema migration. No behavior change at this point — `embedding` is nullable, queries still see only FTS.
2. **Embedding client and tests.** Land the client module and unit tests. No callers yet.
3. **Async indexing.** Land the task, signal, backfill command, and `embeddings_worker` service. New reports start getting embedded; the column gradually populates.
4. **Backfill.** Run `backfill_embeddings` against the existing corpus (manual op, can run for hours/days depending on size — that's fine, it's bounded by `EMBEDDING_BACKFILL_PRIORITY`).
5. **Provider switch.** Replace the body of `radis.pgsearch.providers.search()` and `retrieve()` with the hybrid implementation. At this point hybrid is the new default; rows still missing an embedding participate via the FTS half only.
6. **Monitor.** Watch search latency p95, embedding queue depth, and the rate of "FTS-only fallback" warnings. Tune `HYBRID_VECTOR_TOP_K` / `HYBRID_FTS_MAX_RESULTS` if needed.

Each step is independently mergeable; steps 1–4 ship as quiet infrastructure changes with no user-visible effect, step 5 is the moment hybrid goes live.
