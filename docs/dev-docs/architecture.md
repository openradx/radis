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

RADIS runs as multiple Docker containers deployed using Docker Swarm. In development, these containers run inside a VS Code [Dev Container](https://code.visualstudio.com/docs/devcontainers/create-dev-container) which provides a consistent development environment with Docker-in-Docker support.

### Container Types

**Web Container (`radis-web-1`)**: Runs Django application serving web UI and REST API. Python 3.13 with Daphne ASGI server. Ports: 8000 (dev), 80/443 (prod with SSL). Handles authentication, serves static files, enqueues tasks, and manages database connections. In production, runs with 3 replicas for high availability.

**PostgreSQL Container (`radis-postgres-1`)**: PostgreSQL 17 database storing all data (users, reports, collections, subscriptions, tasks, logs, Procrastinate queue). Port 5432. Uses Docker volumes for persistence.

**Default Worker Container (`radis-default_worker-1`)**: Processes background tasks in the default queue (e.g., extraction job preparation, subscription job preparation, periodic subscription launcher, disk space checks, database backups).

**LLM Worker Container (`radis-llm_worker-1`)**: Executes AI-intensive tasks from the llm queue (extraction tasks, subscription tasks). Uses ChatClient to communicate with LLM service.

**LLM Service Container (`radis-llm_gpu-1`)**: Runs SGLang server with local LLM models for AI-powered features. Uses GPU acceleration when available. Accessible at http://llm.local:8080/v1. Stores model cache in Docker volume.

### Dev Container

The [Dev Container](https://code.visualstudio.com/docs/devcontainers/create-dev-container) is a Docker container that provides the development environment (VS Code, Git, Docker CLI, Node.js, Python tools). It uses Docker-in-Docker to run the application containers inside it. This ensures all developers have identical environments.

## Application Architecture

### Core Django Apps Structure

#### **Search App** (`radis.search`)

- **Purpose**: Report search and retrieval
- **Components**: Search interface, search filters, query parser
- **Key Features**: Text search with query syntax, semantic search support, hybrid search

#### **Collections App** (`radis.collections`)

- **Purpose**: Report organization and bookmarking
- **Components**: Collection model, collection management views
- **Key Features**: Create collections, add/remove reports, organize by project

#### **Notes App** (`radis.notes`)

- **Purpose**: Personal annotations on reports
- **Components**: Note model linked to reports and users
- **Key Features**: Add notes to reports, view all notes, private to user

#### **Subscriptions App** (`radis.subscriptions`)

- **Purpose**: Automated notification system
- **Components**: Subscription model, SubscriptionJob, SubscriptionTask
- **Key Features**: Subscribe to search queries, automatic refresh, email notifications

#### **Extractions App** (`radis.extractions`)

- **Purpose**: AI-powered report analysis
- **Components**: ExtractionJob, ExtractionTask, ExtractionInstance, OutputField
- **Key Features**: Extract structured data from reports using LLMs, custom output fields

#### **Chats App** (`radis.chats`)

- **Purpose**: Interactive AI assistant
- **Components**: Chat interface, ChatClient utility
- **Key Features**: Ask questions about reports, contextual AI responses

#### **PgSearch App** (`radis.pgsearch`)

- **Purpose**: PostgreSQL-based search implementation
- **Components**: Search provider implementation using pg_search and pg_vector
- **Key Features**: Hybrid search (BM25 + semantic), vector embeddings

## Primary Models

### User Management

- **Users & Groups**: Django authentication with group-based access. Each user has an active group that determines which reports they can access.
- **Permissions**: Fine-grained access control for features (extractions, subscriptions, urgent priority).

### Collections & Notes

- **Collection**: User-created report collections with name and owner
- **Note**: Personal annotations on reports, private to each user

### Subscriptions

- **Subscription**: Search criteria for automated notifications
  - Query string and filters (language, modality, date, patient demographics)
  - Email notification preferences
  - Last refresh timestamp
- **SubscriptionJob**: Job to refresh a subscription and find new matching reports
- **SubscriptionTask**: Task processing batch of new reports (questions, email)
- **SubscribedItem**: Individual report matched by subscription

### AI Analysis

- **ExtractionJob**: Job to analyze multiple reports with custom questions
  - Query and filters to select reports
  - Output field definitions
  - Processing status and results
- **ExtractionTask**: Task processing a batch of reports
- **ExtractionInstance**: Individual report extraction result
- **OutputField**: Custom field definition for data extraction (text, number, boolean, etc.)

### Task System

- **AnalysisJob**: Base model for long-running analysis jobs
  - Status tracking (PREPARING, PENDING, IN_PROGRESS, SUCCESS, FAILURE, CANCELED)
  - Owner and group association
  - Email notification settings
  - Urgency flag for priority processing
- **AnalysisTask**: Base model for individual tasks within a job
  - Status tracking with same states as jobs
  - Attempts counter and retry logic
  - Message and log fields
  - Timestamps (created, started, ended)
  - Link to Procrastinate queue job

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

## Background Processing

**Extraction Processing**:

1. Job created with query and output field definitions
2. Job transitions to PREPARING state
3. Search executed to find matching reports
4. Reports batched into tasks
5. Each task processed on LLM queue with configured concurrency
6. Results stored in ExtractionInstance records

**Subscription Processing**:

1. Periodic launcher creates jobs for all subscriptions (every minute by default)
2. Job searches for new reports since last refresh
3. Reports batched into tasks
4. Each task processes reports with subscription questions using LLM
5. New matches added to subscription items
6. Email notifications sent if configured

## Deployment

**Docker Swarm**: Production deployment using Docker Swarm mode for orchestration, scaling, and high availability. Services can be scaled independently (e.g., 3 web replicas).

**Environment Configuration**: Managed through .env files with variables for database credentials, email settings, LLM configuration, SSL certificates.

**CLI Commands**: Helper commands for deployment, backup, and management tasks (compose-up, compose-down, stack-deploy, stack-rm, db-backup, db-restore).

## Key Technologies

- **Backend**: Django 5.1, Python 3.13
- **Database**: PostgreSQL 17 with pg_search and pg_vector extensions
- **Task Queue**: Procrastinate
- **Frontend**: HTMX, Alpine.js, Bootstrap 5
- **Deployment**: Docker, Docker Swarm
