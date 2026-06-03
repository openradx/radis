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
- No automated re-embedding when `EMBEDDING_DIM` changes. That is a manual operator procedure: drop column, re-migrate, defer the embedding orchestrator (see §4.5).
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
│  Async indexing path  (Job/Task orchestrator, periodic-driven)       │
│                                                                      │
│  cron (settings.EMBEDDING_DRAIN_CRON, default nightly 02:00)         │
│        │                                                             │
│        ▼                                                             │
│  embedding_launcher() — `default` queue                              │
│    ├─ queueing_lock="embedding_launcher"                             │
│    ├─ skip if any EmbeddingJob in PREPARING/PENDING/IN_PROGRESS      │
│    ├─ skip if no rows with embedding IS NULL                         │
│    └─ EmbeddingJob.objects.create(...) → job.delay()                 │
│                                                                      │
│  process_embedding_job(job_id) — `default` queue                     │
│    ├─ iterate ReportSearchVector with embedding IS NULL              │
│    ├─ chunk by EMBEDDING_BATCH_SIZE → EmbeddingTask rows             │
│    ├─ task.reports.set(chunk); task.delay()  (no HTTP work)          │
│    └─ job.status = PENDING; return                                   │
│                                                                      │
│  process_embedding_task(task_id) — `embeddings` queue                │
│    ├─ EmbeddingClient.embed_documents([r.body for r in task.reports])│
│    ├─ L2-normalize; bulk_update ReportSearchVector.embedding         │
│    ├─ task.status = SUCCESS/FAILURE; clear queued_job_id             │
│    └─ job.update_job_state()                                         │
│                                                                      │
│  Operator-triggered drain: from a Django shell run                   │
│  `embedding_launcher.defer()` — same code path as periodic.          │
└──────────────────────────────────────────────────────────────────────┘
```

The bulk-upsert API path (`reports/api/viewsets.py:_bulk_upsert_reports`)
already creates `ReportSearchVector` rows with `embedding=NULL` via the FTS
indexing call in its `on_commit` block. The single-create API path goes through
the standard `Report.save()` and the FTS `post_save` signal, which likewise
creates the `ReportSearchVector` row with NULL embedding. Both ingest paths
deposit work into the same DB-resident pending pool; the orchestrator drains it
on the next periodic tick (or on an operator-triggered defer). There is no
per-API-call embedding job.

**Components added inside `radis.pgsearch`:**

| File | Purpose |
|---|---|
| `utils/embedding_client.py` | Sync + async HTTP clients with pluggable backends (`openai`, `ollama`) |
| `migrations/0002_hybrid_search.py` | Single squashed migration: `CREATE EXTENSION vector`; adds `embedding vector(N)` column + HNSW index; creates `EmbeddingJob`/`EmbeddingTask` tables + M2M to `Report`; idempotent `RunPython` for the system user |
| `models.py` (modified) | Adds `embedding` field + `HnswIndex`; defines `EmbeddingJob` and `EmbeddingTask` inheriting `AnalysisJob`/`AnalysisTask` |
| `signals.py` (unchanged from FTS-only) | The FTS `create_or_update_report_search_vector` receiver stays; **no embedding signal** |
| `tasks.py` (modified) | Adds `embedding_launcher` (periodic), `process_embedding_job` (`default` queue), `process_embedding_task` (`embeddings` queue) |
| `providers.py` (modified) | Replaces `search()` and `retrieve()` bodies with hybrid logic |
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

### 4.2 Squashed migration

The entire hybrid-search schema and the system-user data migration land in a
single file, `radis/pgsearch/migrations/0002_hybrid_search.py`, depending on
`pgsearch.0001_initial`, `reports.0013_alter_report_options`,
`procrastinate.0041_post_retry_failed_job`, and `AUTH_USER_MODEL`. Operations
in order:

1. `RunSQL("CREATE EXTENSION IF NOT EXISTS vector;", reverse_sql=RunSQL.noop)`.
   Reverse is a no-op because the extension may be shared with other Postgres
   usage and dropping it would damage unrelated state. Dev rollback is handled
   by recreating the database.
2. `AddField` `embedding` on `ReportSearchVector`:
   `pgvector.django.vector.VectorField(dimensions=settings.EMBEDDING_DIM, null=True)`.
3. `AddIndex` HNSW on `embedding`: `m=16`, `ef_construction=64`,
   `opclasses=["vector_cosine_ops"]`, `name="pgsearch_embedding_hnsw"`.
4. `CreateModel` `EmbeddingJob` (subclass of `AnalysisJob`).
5. `CreateModel` `EmbeddingTask` (subclass of `AnalysisTask`, FK to
   `EmbeddingJob`, M2M to `Report`).
6. `RunPython(create_system_user, reverse_code=RunPython.noop)`: idempotent
   `User.objects.get_or_create(username=settings.EMBEDDING_SYSTEM_USERNAME,
   defaults={"is_active": False, "password": "!"})`. The function is inlined
   at the top of the migration file — no separate helper module — because it
   is only ever called from this one place.

Operation order matters: the `AddField` step references the `vector` type
installed by step 1, and the `CreateModel` steps reference both the `Report`
table (via M2M) and the `AUTH_USER_MODEL` (via owner FK). Step 6 runs last
because it needs the user table to exist (which it does at `0001_initial` of
the auth app, swappable-dependency-ordered above).

Reverse semantics: the auto-generated reverse of steps 2–5 drops the index,
the column, and the two tables. Steps 1 and 6 use `noop` reverse — extension
stays installed; system user stays in the DB. Matches the originally-chained
behaviour exactly.

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

`embedding` is nullable: the row exists from the moment a `Report` is created (FTS path), but its embedding is filled asynchronously by `process_embedding_task` (§6.7). A NULL embedding is treated as "not embedded yet" at query time, and the row participates via the FTS half only.

`save()` on `ReportSearchVector` retains its current behavior of recomputing `search_vector` from `report.body`. The embedding column is written **only** by `process_embedding_task` via `bulk_update()`, never by `save()`, to avoid triggering the FTS signal recursively and to keep the two indexing paths independent.

### 4.5 Operational note on `EMBEDDING_DIM`

pgvector columns and HNSW indexes are bound to a fixed dimension at create time, and HNSW has a 2000-dim ceiling (so `EMBEDDING_DIM ≤ 2000`; Qwen3-Embedding-4B's native 2560 is Matryoshka-truncated client-side). Changing `EMBEDDING_DIM` after deploy requires a manual operator procedure:

1. Drop the HNSW index and the `embedding` column.
2. Re-run `0002_hybrid_search` with the new `EMBEDDING_DIM`. (Because the
   migration is squashed, this single apply re-creates the column at the new
   dim, the HNSW index, and is idempotent for the rest of the operations.)
3. From a Django shell, defer the embedding orchestrator immediately so the
   next nightly tick is not waited for:

   ```python
   from radis.pgsearch.tasks import embedding_launcher
   embedding_launcher.defer()
   ```

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
- `class EmbeddingClient` — sync client used by `process_embedding_task` and the query path.
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
- **Batching:** `embed_documents` sends a single HTTP call per invocation. The higher-level orchestrator (`process_embedding_job`) groups reports into `EmbeddingTask` batches of `EMBEDDING_BATCH_SIZE` before dispatching them to `process_embedding_task`.
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
  GGUF-quantized embedding models produce slightly different vectors than the bf16 reference, so dev embeddings are not interchangeable with prod embeddings. After swapping the model between dev/prod, defer the embedding orchestrator from a Django shell (see §4.5).

## 6. Async indexing (Job/Task orchestrator)

The embedding lifecycle uses the same Job/Task pattern as `ExtractionJob` /
`ExtractionTask` (`radis/extractions/tasks.py:32`) and `SubscriptionJob` /
`SubscriptionTask` (`radis/subscriptions/tasks.py:33`). A periodic launcher
creates one `EmbeddingJob` per drain run; the orchestrator splits pending
reports into `EmbeddingTask` batches; each task is processed on the
`embeddings` queue.

### 6.1 Ingest paths and the pending pool

RADIS has two ingest paths and the orchestrator is decoupled from both. Every
ingest path eventually deposits a `ReportSearchVector` row with
`embedding=NULL`; the launcher consumes that pool on its cron schedule.

- **Single-create** (`POST /api/reports/`) routes through `Report.objects.create`
  in the serializer (`radis/reports/api/serializers.py:87`). The FTS
  `post_save` receiver creates the `ReportSearchVector` row with NULL embedding.
- **Bulk-upsert** (`POST /api/reports/bulk-upsert`) routes through
  `Report.objects.bulk_create` / `bulk_update`
  (`radis/reports/api/viewsets.py:_bulk_upsert_reports`). The bulk path calls
  `enqueue_bulk_index_reports(touched_ids)` in its `on_commit` block, which
  bulk-creates the `ReportSearchVector` rows with NULL embedding.

Accepting a freshness window of hours / next-cycle is the price of batched,
throughput-friendly embedding runs. This design serves all three operational
scenarios with one mechanism:

| Scenario | What happens |
|---|---|
| **Initial bulk upload** (millions of reports via `/bulk-upsert`) | `ReportSearchVector` rows created with `embedding=NULL`. Operator defers the launcher immediately or waits for the next cron tick. One `EmbeddingJob` produces N `EmbeddingTask` batches. |
| **Daily ad-hoc upload** | Reports land NULL via either ingest path. Next periodic tick consolidates the day's pending pool into a single `EmbeddingJob`. |
| **Model-change backfill** | Operator follows §4.5 (drop column, re-migrate), then defers the launcher from a shell. Same code path as the periodic. |

### 6.2 Queue and worker

The `embeddings` Procrastinate queue is served by the `embeddings_worker`
container. The orchestrator (`process_embedding_job`) runs on the `default`
queue alongside `process_extraction_job` and `process_subscription_job`; the
sub-tasks (`process_embedding_task`) run on `embeddings`.

```
./manage.py bg_worker -l debug -q embeddings --autoreload --concurrency 4  # dev
./manage.py bg_worker -l info  -q embeddings --concurrency 4                # prod
```

`embeddings_worker` concurrency tunes parallelism against the embedding
endpoint. Recommended 4; raise if the endpoint has spare throughput, lower if
it rate-limits. The orchestrator does not run on this queue, so there is no
self-deadlock condition tied to concurrency on the `embeddings` queue.

### 6.3 Priorities

Procrastinate priority is "higher = sooner". Embedding work runs at lower
priority than extraction and subscription so it never starves user-driven LLM
operations. The orchestrator (`default` queue) and sub-tasks (`embeddings`
queue) share `EMBEDDING_INDEX_PRIORITY`; there is no separate backfill
priority because the backfill path is the same orchestrator.

| Task | Priority |
|---|---|
| `EXTRACTION_DEFAULT_PRIORITY` (existing) | 2 |
| `EXTRACTION_URGENT_PRIORITY` (existing) | 3 |
| `SUBSCRIPTION_DEFAULT_PRIORITY` (existing) | 3 |
| `SUBSCRIPTION_URGENT_PRIORITY` (existing) | 4 |
| `EMBEDDING_INDEX_PRIORITY` (new) | 0 |

### 6.4 Models

`radis/pgsearch/models.py` defines two new models inheriting `AnalysisJob` and
`AnalysisTask` (`radis/core/models.py:17,220`):

```python
from radis.core.models import AnalysisJob, AnalysisTask


class EmbeddingJob(AnalysisJob):
    default_priority = settings.EMBEDDING_INDEX_PRIORITY
    urgent_priority = settings.EMBEDDING_INDEX_PRIORITY  # no urgent variant

    def delay(self) -> None:
        queued_job_id = app.configure_task(
            "radis.pgsearch.tasks.process_embedding_job",
            allow_unknown=False,
            priority=self.default_priority,
        ).defer(job_id=self.pk)
        self.queued_job_id = queued_job_id
        self.save()


class EmbeddingTask(AnalysisTask):
    job = models.ForeignKey(EmbeddingJob, on_delete=models.CASCADE, related_name="tasks")
    reports = models.ManyToManyField(Report, related_name="embedding_tasks")

    def delay(self) -> None:
        queued_job_id = app.configure_task(
            "radis.pgsearch.tasks.process_embedding_task",
            allow_unknown=False,
            priority=settings.EMBEDDING_INDEX_PRIORITY,
        ).defer(task_id=self.pk)
        self.queued_job_id = queued_job_id
        self.save()
```

**Owner field.** `AnalysisJob.owner` is non-nullable (`settings.AUTH_USER_MODEL`).
Embedding jobs are system-driven and have no human creator. The squashed
`0002_hybrid_search` migration's final `RunPython` step creates
`User(username=settings.EMBEDDING_SYSTEM_USERNAME, is_active=False,
password=unusable)` idempotently; the launcher assigns this user as `owner`
on every `EmbeddingJob`. This avoids subclass-level overrides of `owner` and
keeps the abstract contract clean.

**No `get_absolute_url` in v1.** Existing `ExtractionJob` and `SubscriptionJob`
implement `get_absolute_url` because they have user-facing detail views.
`EmbeddingJob` has no user-facing UI in v1 — operators inspect it via Django
admin (default `ModelAdmin` registration is sufficient). The inherited abstract
`AnalysisJob.get_absolute_url` body is `...`, returning `None`; no call site in
radis treats an `EmbeddingJob` like a user-facing analysis job. A future spec
can add the view and override the method.

`urgent`, `send_finished_mail`, and `finished_mail_template` stay at their
`AnalysisJob` defaults (`False`, `False`, `None`).

### 6.5 Launcher (the periodic task)

`radis/pgsearch/tasks.py`:

```python
@app.periodic(cron=settings.EMBEDDING_DRAIN_CRON)
@app.task(
    queue="default",
    queueing_lock="embedding_launcher",
    pass_context=True,
)
def embedding_launcher(context, timestamp: int) -> None:
    in_flight = EmbeddingJob.objects.filter(
        status__in=[
            EmbeddingJob.Status.PREPARING,
            EmbeddingJob.Status.PENDING,
            EmbeddingJob.Status.IN_PROGRESS,
        ]
    ).exists()
    if in_flight:
        logger.info("EmbeddingJob already in flight; launcher tick is a no-op.")
        return

    has_pending = ReportSearchVector.objects.filter(embedding__isnull=True).exists()
    if not has_pending:
        logger.debug("No reports pending embedding; launcher tick is a no-op.")
        return

    system_user = User.objects.get(username=settings.EMBEDDING_SYSTEM_USERNAME)
    job = EmbeddingJob.objects.create(
        owner=system_user,
        status=EmbeddingJob.Status.PREPARING,
    )
    transaction.on_commit(job.delay)
```

**Two reinforcing layers of duplicate-dispatch prevention:**

- **Procrastinate `queueing_lock="embedding_launcher"`.** While a launcher job
  is in the queue (`todo`) or executing (`doing`), the next cron tick's
  `defer` call silently fails with `AlreadyEnqueued`. The launcher itself is
  fast (one existence check + maybe one INSERT), so the lock is normally
  released within milliseconds.
- **In-flight EmbeddingJob check.** Even if the queueing lock leaks (worker
  crash mid-flight, manual `defer` from a shell, dashboard re-trigger), the
  launcher's first action is to look for any `EmbeddingJob` in a non-terminal
  status. If one exists, the launcher returns without creating another. This
  is the same dedup pattern used by `process_extraction_job` when re-entered
  (`extractions/tasks.py:46`).

### 6.6 Orchestrator (`process_embedding_job`)

```python
@app.task
def process_embedding_job(job_id: int) -> None:
    job = EmbeddingJob.objects.get(id=job_id)
    assert job.status == EmbeddingJob.Status.PREPARING

    # Retry/resume path: tasks already exist, re-enqueue still-pending ones.
    if job.tasks.exists():
        tasks_to_enqueue = job.tasks.filter(status=EmbeddingTask.Status.PENDING)
    else:
        pending_ids_iter = (
            ReportSearchVector.objects
            .filter(embedding__isnull=True)
            .values_list("report_id", flat=True)
            .iterator(chunk_size=10_000)
        )
        batch: list[int] = []
        for report_id in pending_ids_iter:
            batch.append(int(report_id))
            if len(batch) >= settings.EMBEDDING_BATCH_SIZE:
                _create_embedding_task(job, batch)
                batch = []
        if batch:
            _create_embedding_task(job, batch)

        tasks_to_enqueue = job.tasks.filter(status=EmbeddingTask.Status.PENDING)

    job.status = EmbeddingJob.Status.PENDING
    job.queued_job_id = None
    job.save()

    for task in tasks_to_enqueue:
        if not task.is_queued:
            task.delay()


def _create_embedding_task(job: EmbeddingJob, report_ids: list[int]) -> EmbeddingTask:
    task = EmbeddingTask.objects.create(job=job, status=EmbeddingTask.Status.PENDING)
    task.reports.set(Report.objects.filter(pk__in=report_ids))
    return task
```

Mirrors `process_extraction_job` (`extractions/tasks.py:32`). State transitions
follow the standard pattern:

- `PREPARING` while tasks are being created (sub-tasks must not be dispatched yet).
- `PENDING` after task creation completes; sub-tasks are then enqueued.
- `IN_PROGRESS` / `SUCCESS` / `WARNING` / `FAILURE` driven by `update_job_state`
  (inherited from `AnalysisJob`) called from each sub-task on completion.

The orchestrator does no HTTP work. For 1M pending reports at
`EMBEDDING_BATCH_SIZE=32`, it creates ~31,250 `EmbeddingTask` rows and defers
them — well under a minute on the `default` worker. Its slot is freed
immediately after; long-running embedding work happens on the `embeddings`
worker.

### 6.7 Sub-task (`process_embedding_task`)

```python
@app.task(queue="embeddings")
def process_embedding_task(task_id: int) -> None:
    task = EmbeddingTask.objects.get(id=task_id)
    task.status = EmbeddingTask.Status.IN_PROGRESS
    task.started_at = timezone.now()
    task.attempts = task.attempts + 1
    task.save()

    client = EmbeddingClient()
    try:
        report_ids = list(task.reports.values_list("pk", flat=True))
        rsvs = list(
            ReportSearchVector.objects
            .filter(report_id__in=report_ids)
            .select_related("report")
            .only("id", "report_id", "report__body")
        )
        texts = [rsv.report.body for rsv in rsvs]
        vectors = client.embed_documents(texts)
        for rsv, vec in zip(rsvs, vectors, strict=True):
            rsv.embedding = vec
        ReportSearchVector.objects.bulk_update(rsvs, fields=["embedding"])

        task.status = EmbeddingTask.Status.SUCCESS
    except EmbeddingClientError as exc:
        logger.exception("Embedding task %s failed: %s", task_id, exc)
        task.status = EmbeddingTask.Status.FAILURE
        task.message = str(exc)
        raise  # Procrastinate retry policy applies
    finally:
        task.ended_at = timezone.now()
        task.queued_job_id = None
        task.save()
        task.job.update_job_state()
        client.close()
```

Raising on `EmbeddingClientError`
lets Procrastinate's retry policy apply. After retries exhaust, the exception
propagates, the task ends as `FAILURE`, and `update_job_state` is still called
from the `finally` block. The job finishes with status `WARNING` (some tasks
failed, some succeeded) or `FAILURE` (all failed). The next launcher tick will
create a fresh job that picks up any rows still NULL.

### 6.8 Operator-triggered drain

The only ingest-time signal is the FTS `post_save` receiver
(`create_or_update_report_search_vector`), which creates the
`ReportSearchVector` row with `embedding=NULL`. Embedding is driven entirely
by the orchestrator from then on.

Operators trigger an immediate drain — typically after a model swap or initial
bulk import — by deferring the same launcher from a Django shell:

```python
from radis.pgsearch.tasks import embedding_launcher
embedding_launcher.defer()
```

This goes through the same launcher → orchestrator → sub-task path as the
periodic; the only difference is who fires it. One code path, one set of
tests, one observable lifecycle.

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
EMBEDDING_DRAIN_CRON = "0 2 * * *"  # nightly; bump in code for faster dev drain
EMBEDDING_REQUEST_TIMEOUT = 30  # seconds
EMBEDDING_MAX_INPUT_CHARS = 60_000
EMBEDDING_QUERY_INSTRUCTION = (
    "Instruct: Given a radiology search query, retrieve relevant radiology reports.\n"
    "Query: "
)
EMBEDDING_BATCH_SIZE = 32

EMBEDDING_INDEX_PRIORITY = 0
EMBEDDING_SYSTEM_USERNAME = "system"

HYBRID_VECTOR_TOP_K    = 100
HYBRID_FTS_MAX_RESULTS = 10_000
HYBRID_RRF_K           = 60
```

These are tuning constants. Changing them is a code change with a PR diff. This matches the project's existing pattern (`EXTRACTION_LLM_CONCURRENCY_LIMIT = 6`, the `CHAT_*_SYSTEM_PROMPT` blocks). `EMBEDDING_SYSTEM_USERNAME` names the system user that owns every auto-generated `EmbeddingJob`; the squashed migration's `RunPython` step creates this user idempotently.

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
| Embedding service down during a sub-task | `process_embedding_task` raises; Procrastinate retries with exponential backoff; `embedding` stays NULL | WARNING per attempt, ERROR after final retry |
| Launcher fires while EmbeddingJob is `PREPARING`/`PENDING`/`IN_PROGRESS` | Status check returns immediately; tick is a no-op | INFO |
| Orchestrator crashes during task creation (partial dispatch) | Job stays in `PREPARING`. Next launcher tick sees in-flight job and no-ops. Operator marks job `FAILURE` in admin to allow a fresh run | ERROR + operator action |
| Sub-task fails after Procrastinate retries exhausted | Task ends as `FAILURE`. `update_job_state` rolls the job to `WARNING` (some tasks succeeded) or `FAILURE` (all failed). NULL rows remain; next launcher creates a new job to retry them | ERROR |
| `embeddings_worker` saturation | Sub-tasks queue up; orchestrator already returned. No deadlock; just slower drain | DEBUG |
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
- `process_embedding_task` logs at INFO: batch size, total chars, latency, success/retry counts.
- `embedding_launcher` and `process_embedding_job` log status transitions and dispatch counts at INFO.
- Operators inspect job/task state via Django admin (`EmbeddingJob`, `EmbeddingTask` use the default `ModelAdmin`).
- The existing OpenTelemetry overlay (commit `653e0c67`) tags telemetry per service; `embeddings_worker` shows up automatically.

## 10. Testing strategy

### 10.1 Unit tests (no DB)

| File | Coverage |
|---|---|
| `tests/unit/test_embedding_client.py` | Backend payload/response round-trip, path override, instruction prefix, normalization, dim validation, all error modes, truncation |
| `tests/unit/test_provider_fusion.py` | `_rrf_fuse(vec_rank, fts_rank, k)` pure-Python helper: disjoint, overlapping, FTS-only, vector-only, both-empty, tiebreak by report_id |
| `tests/unit/test_embedding_launcher.py` | No-op when EmbeddingJob already in flight; no-op when no rows pending; happy path creates job and calls `delay`; raises if system user missing |
| `tests/unit/test_process_embedding_job.py` | Batches pending reports into `EmbeddingTask` rows of size `EMBEDDING_BATCH_SIZE`; status transitions `PREPARING` → `PENDING`; retry/resume path re-enqueues only `PENDING` tasks; empty pool exits cleanly |
| `tests/unit/test_process_embedding_task.py` | Embeds reports, writes vectors, sets status `SUCCESS`; status `FAILURE` and re-raise on `EmbeddingClientError`; calls `job.update_job_state` in both paths; clears `queued_job_id` |

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

Documented in §5.4. Mitigated by deferring `embedding_launcher` after a model swap (see §4.5). The next drain re-embeds everything.

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

1. **Schema, dependency, models, data migration.** Land the `pgvector` Python dep and the squashed `0002_hybrid_search` migration (extension + embedding column + HNSW + `EmbeddingJob`/`EmbeddingTask` tables + system user). No behaviour change yet — `embedding` is nullable, queries still see only FTS.
2. **Embedding client and tests.** Land the client module and unit tests. No callers yet.
3. **Orchestrator tasks and `embeddings_worker`.** Land `embedding_launcher`, `process_embedding_job`, `process_embedding_task`, and the `embeddings_worker` container (with `--concurrency 4`). The launcher starts ticking on its compile-time `EMBEDDING_DRAIN_CRON` schedule; with no rows yet, all ticks no-op.
4. **Initial drain.** From a shell, run `embedding_launcher.defer()` so the orchestrator picks up the existing corpus. This is the only "operator action" in the rollout. It runs at `EMBEDDING_INDEX_PRIORITY` and lives behind whatever other work is on the queues; it can run for hours to days on a large corpus.
5. **Provider switch.** Replace the body of `radis.pgsearch.providers.search()` and `retrieve()` with the hybrid implementation. At this point hybrid is the new default; rows still missing an embedding participate via the FTS half only.
6. **Monitor.** Watch search latency p95, embedding-queue depth, `EmbeddingJob` admin state, and the rate of "FTS-only fallback" warnings. Tune `HYBRID_VECTOR_TOP_K` / `HYBRID_FTS_MAX_RESULTS` if needed.

Each step is independently mergeable; steps 1–3 ship as quiet infrastructure changes with no user-visible effect, step 4 starts populating the column, step 5 is the moment hybrid goes live.
