# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

RADIS (Radiology Report Archive and Discovery System) is a Django-based web application for managing, retrieving, and analyzing radiology reports in hospital environments. It features hybrid search (full-text + semantic), LLM integration for AI-powered analysis, and a subscription system for report notifications.

**Status**: Early development (v0.0.0) - research purposes only, not a certified medical device
**License**: AGPL 3.0 or later

## Essential Commands

All commands use the `cli.py` wrapper (via Typer). Use `uv run cli <command>` from project root.

```bash
# Development setup
uv sync                              # Install dependencies
cp ./example.env ./.env              # Create environment file
uv run cli compose-up -- --watch     # Start dev server with hot reload
uv run cli compose-down              # Stop containers

# Code quality
uv run cli lint                      # Run linting (ruff + djlint)
uv run cli format-code               # Format code with ruff

# Testing
uv run cli test                      # Run all tests
uv run cli test -- --cov             # Run with coverage
uv run cli test -- -k test_name      # Run specific test by name
uv run cli test -- -x                # Stop on first failure
uv run pytest radis/reports/tests/   # Run tests in specific directory

# Utilities
uv run cli shell                     # Django shell
uv run cli generate-example-reports --count 10  # Generate test data with LLM
uv run cli db-backup                 # Backup database
```

## Architecture

### Tech Stack

- **Backend**: Python 3.12+, Django 6.0+, PostgreSQL 17
- **Search**: pg_vector (semantic), pg_search (full-text), hybrid ranking
- **Async**: Daphne (ASGI), Django Channels, Procrastinate (task queue)
- **Frontend**: Django templates, Cotton components, HTMX, Alpine.js, Bootstrap 5
- **LLM**: External OpenAI-compatible API endpoint
- **API**: Django REST Framework with async support (ADRF)

### Django Apps

- **radis.core/**: Core functionality, UI layouts, abstract base classes. Models: `AnalysisJob`, `AnalysisTask` (abstract bases for job/task pattern).
- **radis.reports/**: Report management and REST API. Models: `Report` (main entity with patient info, study metadata, body text), `Language`, `Modality`, `Metadata`.
- **radis.search/**: Full-text and semantic search interface. Contains `SearchView`, `SearchForm`, `QueryParser` for complex query syntax, and provider registry.
- **radis.pgsearch/**: PostgreSQL search implementation. Implements search provider interface with hybrid ranking (full-text + vector).
- **radis.subscriptions/**: Notification system for new reports matching criteria. Background tasks check new reports against user subscriptions.
- **radis.collections/**: Report bookmarking and organization into custom collections.
- **radis.notes/**: User annotations on reports for adding context.
- **radis.chats/**: Chat functionality for interacting with reports using LLM.
- **radis.extractions/**: Data extraction from reports using LLM. Models: `ExtractionJob`, `ExtractionTask`.
- **radis.labels/**: LLM auto-labeling of reports. A per-group Yes/No gate screens applicability, then each active label is classified into one of five buckets (`PRESENT`/`LIKELY`/`POSSIBLE`/`ABSENT`/`UNMENTIONED`); the three surfacing buckets drive report-detail badges and the label filter in the search Filters panel. Models: `LabelGroup`, `Label`, `LabelResult`, `GateAnswer`, `LabelingScanCheckpoint`, `LabelingJob`, `LabelingTask`.

Shared utilities come from `adit-radis-shared` package (accounts, token auth, common utilities).

### Job/Task Processing Model

Analysis operations follow a Job -> Task pattern (similar to ADIT):

- An **AnalysisJob** contains multiple **AnalysisTasks**
- Status flow: `UNVERIFIED` -> `PREPARING` -> `PENDING` -> `IN_PROGRESS` -> `SUCCESS`/`WARNING`/`FAILURE`/`CANCELED`
- Jobs automatically update state based on task completion
- Email notifications sent on job completion
- Background workers (Procrastinate) process tasks from `default` and `llm` queues

### Search Architecture

- **Provider system**: Plugin-based architecture (currently PostgreSQL, extensible for Vespa/ElasticSearch)
- **QueryParser**: Parses complex queries with operators, field filters, and boolean logic
- **Hybrid search**: Combines full-text search with semantic vector similarity
- **Ranking**: Results ranked by relevance score combining both search methods

### Docker Services

- **web**: Django dev server with Daphne (port 8000)
- **default_worker**: General background task processor (Procrastinate queue: `default`)
- **llm_worker**: LLM-specific task processor (Procrastinate queue: `llm`)
- **embeddings_worker**: Embedding task processor (Procrastinate queue: `embeddings`)
- **postgres**: PostgreSQL 17 with pg_vector and pg_search extensions (port 5432)

### LLM Endpoint

App services talk to the endpoint in `LLM_BASE_URL`; the stack contains no inference service. In development that is by default a provider on the Docker host (`http://host.docker.internal:11434/v1`), and the dev compose file sets `extra_hosts: host.docker.internal:host-gateway` so the name resolves on plain Linux Docker too. See `docs/dev-docs/contributing.md` for running Ollama locally, natively or as a standalone container.

## Environment Variables

Key variables in `.env` (see `example.env`):

- `ENVIRONMENT`: `development` or `production`
- `DJANGO_SECRET_KEY`: Cryptographic signing key
- `POSTGRES_PASSWORD`: Database password
- `DJANGO_ALLOWED_HOSTS`: Comma-separated allowed hosts
- `LLM_BASE_URL`: Base URL of the OpenAI-compatible LLM endpoint (required, no default)
- `LLM_API_KEY`: API key for that endpoint (many self-hosted providers ignore it)
- `LLM_DEFAULT_MODEL`: Model every feature uses unless overridden (required). Takes the form `model[?param=value&...]`; the params are merged into the request body, values are read as JSON where possible (`temperature=0` → number, `reasoning_effort=none` → string), and dotted keys nest (`chat_template_kwargs.enable_thinking=false`)
- `LLM_CHATS_MODEL`, `LLM_QUERY_GENERATION_MODEL`, `LLM_EXTRACTIONS_MODEL`, `LLM_SUBSCRIPTIONS_MODEL`, `LLM_LABELING_MODEL`: Per-feature overrides, same form. Blank means use `LLM_DEFAULT_MODEL`

- `SITE_NAME`, `SITE_DOMAIN`: Site framework settings
- `ADMIN_USERNAME`, `ADMIN_EMAIL`, `ADMIN_PASSWORD`: Initial superuser

Hybrid search embeddings (`radis.pgsearch`):

- `EMBEDDINGS_MODEL`: Embedding model, same `model[?param=value&...]` form as the LLM
  models. Empty means full-text search only — no embedding jobs, no calls to the service
- `EMBEDDINGS_BASE_URL`, `EMBEDDINGS_API_KEY`: Default to `LLM_BASE_URL` / `LLM_API_KEY`.
  Set only when embeddings are served from a different endpoint, which is the normal case
  for vLLM and SGLang since they serve one model per process
- `EMBEDDINGS_DIM`: Vector width (default `1024`). Schema-coupled — it must match the
  pgsearch migrations and any `dimensions` parameter in `EMBEDDINGS_MODEL`, both checked
  at startup (`pgsearch.E001`, `pgsearch.E003`)
- `EMBEDDINGS_QUERY_INSTRUCTION`: Instruction prefix prepended to search queries.
  Model-specific; not a request parameter, so it is not part of the model spec
- `EMBEDDINGS_REQUEST_TIMEOUT_SECONDS`: Defaults to `LLM_REQUEST_TIMEOUT_SECONDS` (itself
  60s by default), not to a fixed value — raising the LLM timeout raises this one too
  unless set here explicitly
- `EMBEDDINGS_QUERY_CACHE_TIMEOUT_SECONDS`: How long a search query's embedding stays in
  the Django cache (default `900`s). The knob to reach for right after a model/provider
  swap — cached query vectors otherwise keep serving stale results for up to this long
- `EMBEDDINGS_BATCH_SIZE`, `EMBEDDINGS_SUBJOB_SIZE`, `EMBEDDINGS_WORKER_CONCURRENCY`:
  Throughput tuning

Auto-labeling (`radis.labels`):

- `LABELING_SYSTEM_PROMPT`: Generic per-label classification prompt (only `$report` is substituted). Has a built-in default.
- `LABELING_GATE_SYSTEM_PROMPT`: Generic group gate (Yes/No) prompt. Has a built-in default.
- `LABELING_JOB_PRIORITY`: Procrastinate priority for labeling jobs (default `1`).
- `LABELING_TASK_BATCH_SIZE`: Reports per labeling task (default `100`).
- `LABELING_LLM_CONCURRENCY_LIMIT`: Max concurrent LLM calls per task (default `2`).
- `LABELING_GATE_BATCH_SIZE`: Groups screened per gate batch (default `10`).
- `LABELING_SCAN_CRON`: Cron for the periodic incremental scan (default `0 2 * * *`).

Worker-crash recovery (`radis.core`):

- `ANALYSIS_STALLED_WORKER_GRACE_SECONDS`: Heartbeat silence before a worker counts as dead when repairing stale analysis tasks (default `30`; must never be set below 30 — Procrastinate itself declares workers stalled at 30 s).
- `ANALYSIS_SWEEP_CRON`: Cron for the periodic sweep that repairs tasks left `IN_PROGRESS` by killed workers (default `* * * * *`).

Labeling uses the shared core LLM client (`radis.core.utils.llm_client`); its timeout, rate-limit gate, and transient-retry knobs are the global `LLM_REQUEST_TIMEOUT_SECONDS`, `LLM_RATE_LIMIT_*`, and `LLM_TRANSIENT_RETRY_*` settings.

## Code Standards

- **Style Guide**: Google Python Style Guide
- **Line Length**: 100 characters (Ruff), 120 for templates (djlint)
- **Type Checking**: pyright in basic mode (migrations excluded)
- **Linting**: Ruff with E, F, I, DJ rules

## Key Dependencies

- **adit-radis-shared**: Shared infrastructure (accounts, token auth, CLI commands, UI components)
- **radis-client/**: Official Python client library for API access (included in repo)
- **pgvector**: PostgreSQL extension for vector similarity search
- **procrastinate**: PostgreSQL-backed async task queue
- **channels/daphne**: WebSocket support for real-time features
- **openai**: Client for OpenAI-compatible LLM APIs

## Testing

- **Framework**: pytest with pytest-django, pytest-playwright, pytest-asyncio
- **Acceptance tests**: Marked with `@pytest.mark.acceptance`, require dev containers
- **Test locations**: `radis/*/tests/` directories within each app
- **Factories**: factory-boy with Faker for test data generation
- **Timeout**: 60 seconds per test

## API Examples

Using `radis-client` for programmatic access:

```python
from radis_client import RadisClient

# Initialize client
client = RadisClient(server_url="https://radis.example.com", auth_token="your-token")

# Create a new report
report = client.create_report({
    "document_id": "DOC-12345",
    "patient_id": "PAT-001",
    "patient_birth_date": "1980-01-15",
    "patient_sex": "M",
    "study_datetime": "2024-03-15T10:30:00",
    "study_description": "CT Thorax",
    "body": "Findings: No acute abnormality...",
    "groups": ["radiology"]
})

# Retrieve a report
report = client.retrieve_report("DOC-12345", full=True)

# Update a report (with upsert)
client.update_report("DOC-12345", {"body": "Updated findings..."}, upsert=True)
```

### Search via API

```python
import requests

response = requests.get(
    "https://radis.example.com/api/reports/",
    headers={"Authorization": "Token your-token"},
    params={"search": "pneumonia CT thorax", "limit": 50}
)
reports = response.json()
```

## Troubleshooting

### Search Not Returning Expected Results

- Check PostgreSQL extensions are installed: `pg_vector`, `pg_search`
- Verify report has `body` text indexed
- Check search provider is configured in settings
- Review QueryParser syntax for complex queries

### LLM Operations Failing

- Verify `LLM_BASE_URL` and `LLM_DEFAULT_MODEL` are set (`LLM_API_KEY` is optional; many self-hosted providers ignore it)
- Check the right logs for the failing path: chat and query generation run in `web` (`docker compose logs web`), while extraction, subscription and labeling tasks run in `llm_worker` (`docker compose logs llm_worker`)
- Ensure the endpoint is reachable from inside the containers, not just from the host.
  Note the single quotes, so the variables are expanded in the container and not by your shell:
  `docker compose exec web sh -c 'curl -sf -H "Authorization: Bearer $LLM_API_KEY" "$LLM_BASE_URL/models"'`
- If the endpoint runs on the host, it must listen on more than loopback: Ollama needs
  `OLLAMA_HOST=0.0.0.0`, otherwise containers get "connection refused" even though the name resolves
- If the provider rejects requests with a 400, check the `?param=value` part of the model setting for parameters it doesn't support
- Confirm the endpoint serves the configured model and supports structured outputs
- If one feature misbehaves, check whether it has its own `LLM_<FEATURE>_MODEL` override

### Hybrid Search Returns Only Full-Text Results

- Confirm `EMBEDDINGS_MODEL` is set — empty is the documented way to run FTS-only
- Check `docker compose logs embeddings_worker` for failed subjobs
- Reports ingested before the model was configured have no vector; run
  `docker compose exec web ./manage.py embed_pending` to backfill them
- A search logs a WARNING and degrades to FTS-only when the embedding service is rate
  limiting or unreachable; the log line names which
- Verify the endpoint serves the model: `docker compose exec web sh -c 'curl -sf -H
  "Authorization: Bearer ${EMBEDDINGS_API_KEY:-$LLM_API_KEY}"
  "${EMBEDDINGS_BASE_URL:-$LLM_BASE_URL}/models"'`
- Leaving `EMBEDDINGS_BASE_URL` unset (now the normal case, since it inherits
  `LLM_BASE_URL`) can point embeddings at an endpoint that serves chat but not
  `/v1/embeddings` — a gateway route that only forwards chat, or an Ollama where the
  embedding model was never `ollama pull`ed. This surfaces as `openai.NotFoundError` or
  `openai.BadRequestError`, logged differently depending on the path: a search request
  (`docker compose logs web`) logs the full traceback once per `(EMBEDDINGS_BASE_URL,
  MODEL)` configuration, then a single-line WARNING on every search after that until the
  configuration changes — one traceback followed by repeating WARNINGs is this working as
  designed, not a new problem. An embedding subjob (`docker compose logs
  embeddings_worker`) is a separate code path with no such throttle, so each subjob still
  logs its own error on every invocation. Fix by pulling/serving the embedding model on
  that endpoint, or by setting `EMBEDDINGS_BASE_URL` explicitly to an endpoint that does
  serve `/v1/embeddings`.

### Worker Not Processing Tasks

- Check worker logs: `docker compose logs default_worker`
- Verify Procrastinate is running: `docker compose ps`
- Check PostgreSQL connection
- Ensure task is in correct queue (`default` vs `llm` vs `embeddings`)

### Report Import Issues

- Validate document_id is unique
- Check required fields: document_id, patient_id, body
- Verify group exists and user has access
- Check date formats match ISO 8601

### Subscription Notifications Not Sending

- Verify email settings in environment variables
- Check subscription criteria matches new reports
- Review subscription task logs
- Ensure background worker is processing subscription queue

### Labels Not Appearing

- Confirm the label exists and is `active`
- Ensure a backfill has run or the periodic scan (`LABELING_SCAN_CRON`) has ticked since the label/report was created
- Check the group gate was answered `YES` for the report (a `NO` gate skips per-label classification)
- Verify the result is a surfacing bucket (`PRESENT`/`LIKELY`/`POSSIBLE`); `ABSENT`/`UNMENTIONED` never surface
- Use `uv run cli shell` + `labels_status` (or `manage.py labels_status`) to inspect corpus-wide counts and the scan checkpoint
