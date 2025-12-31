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

- **Backend**: Python 3.12+, Django 5.1+, PostgreSQL 17
- **Search**: pg_vector (semantic), pg_search (full-text), hybrid ranking
- **Async**: Daphne (ASGI), Django Channels, Procrastinate (task queue)
- **Frontend**: Django templates, Cotton components, HTMX, Alpine.js, Bootstrap 5
- **LLM**: OpenAI-compatible API or local Llama.cpp server
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

Shared utilities come from `adit-radis-shared` package (accounts, token auth, common utilities).

### Job/Task Processing Model

Analysis operations follow a Job -> Task pattern (similar to ADIT):

- An **AnalysisJob** contains multiple **AnalysisTasks**
- Status flow: `UNVERIFIED` -> `PREPARING` -> `PENDING` -> `IN_PROGRESS` -> `SUCCESS`/`WARNING`/`FAILURE`
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
- **postgres**: PostgreSQL 17 with pg_vector and pg_search extensions (port 5432)

### Docker Compose Profiles

- **No profile**: Uses external LLM via API (configure `OPENAI_API_BASE_URL`)
- **cpu**: Local LLM on CPU using Llama.cpp
- **gpu**: Local LLM with CUDA acceleration using Llama.cpp

## Environment Variables

Key variables in `.env` (see `example.env`):

- `ENVIRONMENT`: `development` or `production`
- `DJANGO_SECRET_KEY`: Cryptographic signing key
- `POSTGRES_PASSWORD`: Database password
- `DJANGO_ALLOWED_HOSTS`: Comma-separated allowed hosts
- `OPENAI_API_KEY`: API key for OpenAI-compatible LLM service
- `OPENAI_API_BASE_URL`: Base URL for LLM API (for local or alternative providers)
- `LLM_MODEL_NAME`: Model to use for LLM operations
- `SITE_NAME`, `SITE_DOMAIN`: Site framework settings
- `ADMIN_USERNAME`, `ADMIN_EMAIL`, `ADMIN_PASSWORD`: Initial superuser

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

- Verify `OPENAI_API_KEY` and `OPENAI_API_BASE_URL` are set
- Check llm_worker is running: `docker compose logs llm_worker`
- For local LLM, ensure llama.cpp service is healthy
- Review model compatibility (see KNOWLEDGE.md for recommendations)

### Worker Not Processing Tasks

- Check worker logs: `docker compose logs default_worker`
- Verify Procrastinate is running: `docker compose ps`
- Check PostgreSQL connection
- Ensure task is in correct queue (`default` vs `llm`)

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
