# **RADIS Architecture Documentation**

This document provides a comprehensive overview of RADIS's architecture, implementation details, and key components for developers.

## System Overview

RADIS (Radiology Report Archive and Discovery System) is a full-stack web application for managing and searching radiology reports. The system consists of a Django-based backend, PostgreSQL database with full-text search extensions, and server-side rendered web interface enhanced with HTMX for dynamic interactions.

RADIS inherits common functionality from **ADIT Radis Shared**, a shared library that provides core components including user authentication, token-based authentication, common utilities, and shared Django applications used by both ADIT and RADIS projects.

## High-Level Architecture

The RADIS platform provides report management, advanced search, AI-powered analysis, and collaborative features through coordinated Docker containers. Users access the system via **web browser** or **RADIS Client** (Python library for programmatic access), performing operations such as searching reports, creating collections, managing subscriptions, and analyzing reports with AI.

The system consists of three main components: a Django API server handling web UI and orchestration, a PostgreSQL database storing all persistent data and serving as the task queue, and background workers executing long-running AI operations.

## Backend Architecture

**Django Web/API Server**: Central coordination engine providing REST API endpoints, authentication, user/session management, static assets, and task orchestration. Creates job/task records in PostgreSQL and schedules background work.

**PostgreSQL Database**: System of record storing user accounts, reports, collections, subscriptions, task queue entries, execution history, and search indexes. Uses pg_search and pg_vector extensions for hybrid search capabilities.

**Background Workers**: Docker containers polling PostgreSQL for tasks, executing AI-powered extractions and subscription processing using LLMs.

### Procrastinate Task Queue System

RADIS uses [Procrastinate](https://procrastinate.readthedocs.io/en/stable/), a PostgreSQL-based task queue storing jobs directly in the database without external message brokers. Tasks are Python functions with decorators, supporting job scheduling, prioritization, retry logic, cancellation, and periodic task execution.

**RADIS Task Types**:

- **Default Queue**: `process_extraction_job`, `process_subscription_job`, `subscription_launcher` (periodic), `check_disk_space`, `backup_db`
- **LLM Queue**: `process_extraction_task`, `process_subscription_task` (AI-intensive operations)

## Frontend Architecture

**Web UI**: Server-side rendered with Django templates and HTMX for dynamic interactions. Uses Bootstrap 5 for styling and Alpine.js for interactive components.

**RADIS Client**: Python package (`radis-client`) for programmatic API access, supporting report creation and search operations.

## Docker Container Architecture

**Docker Swarm**: RADIS employs a sophisticated multi-container architecture, optimized for local deployment using Docker Swarm mode—a feature included with all Docker installations. This local-first approach ensures compliance with the strict data security requirements inherent in hospital and research environments where sensitive patient or research data is managed. By leveraging Docker Swarm, RADIS offers seamless scalability, allowing services to be easily adjusted to meet the specific computational demands of the deployment site.

### Container Types

**Web Container (`radis-web-1`)**: Runs Django application serving web UI and REST API. Ports: 8000 (dev), 80/443 (prod with SSL). Handles authentication, serves static files, enqueues tasks, and manages database connections. In production, runs with 3 replicas for high availability.

**PostgreSQL Container (`radis-postgres-1`)**: PostgreSQL database storing all data (users, reports, collections, subscriptions, tasks, logs, Procrastinate queue). Port 5432. Uses Docker volumes for persistence.

**Default Worker Container (`radis-default_worker-1`)**: Processes background tasks in the default queue (e.g., extraction job preparation, subscription job preparation, periodic subscription launcher, disk space checks, database backups).

**LLM Worker Container (`radis-llm_worker-1`)**: Executes AI-intensive tasks from the llm queue (extraction tasks, subscription tasks). Uses ChatClient to communicate with LLM service.

**LLM Service Container (`radis-llm_gpu-1`)**: Runs llama.cpp server with local LLM models for AI-powered features. Llama.cpp provides OpenAI-compatible API endpoints for chat completions and structured output via JSON schema (using `response_format` parameter). Uses GPU acceleration when available (CUDA support). Accessible at http://llm.local:8080/v1. Stores model cache in Docker volume. Configured with context size of 8192 tokens, 2 parallel slots, and 99 GPU layers for maximum GPU utilization.

### LLM Configuration

**Model-Agnostic Architecture**: RADIS is model-agnostic and works with any LLM that provides an OpenAI-compatible API, supporting both local and external providers.

**Development**: Uses **llama.cpp** server with GGUF-formatted models. Default model: `SmolLM2-135M-Instruct` (lightweight for testing). Supports CPU and GPU modes. Models automatically downloaded from HuggingFace and cached in Docker volume.

**Production**: Uses **SGLang** server for optimized inference with better batching and throughput.

**Structured Output**: Uses OpenAI's `beta.chat.completions.parse` API with Pydantic schemas as `response_format` parameter, ensuring LLM returns valid JSON matching defined schemas. Applied in extractions (custom field extraction) and subscriptions (yes/no question filtering).

**External Providers**: Optionally use external APIs (OpenAI GPT-4, Claude, Azure OpenAI, local Ollama) by configuring `EXTERNAL_LLM_PROVIDER_URL` and `EXTERNAL_LLM_PROVIDER_API_KEY` environment variables.

## Search Architecture

RADIS uses a modular search architecture allowing different search providers to be plugged in:

**Search Provider Interface**: Defines search, retrieval, and indexing operations

- **PgSearch Provider**: Default implementation using PostgreSQL full-text search with pg_search and pg_vector extensions
- **Alternative Providers**: Vespa and ElasticSearch can be integrated through the same interface

**Query Parser**: Parses user queries with support for:

- AND/OR operators
- Phrase search ("exact match")
- Exclusion (-term)
- Case-insensitive matching

**Search Filters**: Applied on top of query:

- Language, modalities, study date range
- Study description, patient sex, patient age range
- Patient ID, group access
- Created after timestamp (for subscriptions)
