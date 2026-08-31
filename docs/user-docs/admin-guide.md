# Admin Guide

The Admin Guide is intended for system administrators and technical staff responsible for configuring and maintaining RADIS (Radiology Report Archive and Discovery System).

## Installation

On the server RADIS lives in a production folder that holds the checkout and the `.env` file. The name is up to you; `radis_prod` below is only an example, use whatever you called your production folder wherever this guide says "production folder":

```terminal
git clone https://github.com/openradx/radis.git radis_prod
cd radis_prod  # or whatever you named your production folder
uv sync
cp ./example.env ./.env  # set ENVIRONMENT=production and adjust the variables (see below)
uv run cli compose-pull  # pulls the Docker image (RADIS_IMAGE, default ghcr.io/openradx/radis:latest)
uv run cli stack-deploy  # starts the Docker Swarm stack
```

`stack-deploy` refuses to run unless `ENVIRONMENT=production` is set in `.env`. It does not build anything; the stack runs the pre-built image named by `RADIS_IMAGE`.

### Environment Variables

All settings are read from `.env`; the comments in `example.env` describe every variable. For production at least set:

- `ENVIRONMENT=production`
- Secrets: `DJANGO_SECRET_KEY`, `POSTGRES_PASSWORD`, `TOKEN_AUTHENTICATION_SALT`, `SUPERUSER_PASSWORD`, `SUPERUSER_AUTH_TOKEN` (generate with `uv run cli generate-django-secret-key`, `generate-secure-password`, `generate-auth-token`)
- Hosts: `DJANGO_ALLOWED_HOSTS`, `DJANGO_CSRF_TRUSTED_ORIGINS`, `SITE_DOMAIN`, `SITE_NAME`
- LLM: `LLM_BASE_URL`, `LLM_DEFAULT_MODEL` (both required, RADIS refuses to start without them; see [LLM and Embeddings Configuration](#llm-and-embeddings-configuration)). The `host.docker.internal` default from `example.env` is a development convenience that is not injected into the production stack, so point `LLM_BASE_URL` at an endpoint the stack's nodes can reach. `EMBEDDINGS_MODEL` is optional; leave it empty for full-text search only
- SSL: `SSL_SERVER_CERT_FILE`, `SSL_SERVER_KEY_FILE`, `SSL_SERVER_CHAIN_FILE` (`uv run cli generate-certificate-chain` builds the chain from your CA-signed certificate)
- Email: `DJANGO_EMAIL_URL`, `DJANGO_SERVER_EMAIL`, `DJANGO_ADMIN_EMAIL`, `DJANGO_ADMIN_FULL_NAME`, `SUPPORT_EMAIL`
- Folders: `BACKUP_DIR` (see [Backups](../backups.md))

Optional tuning: `RADIS_IMAGE` and `STACK_NAME` (a second stack such as staging on the same host), `BACKUP_CRON` and `BACKUP_ENABLED`, `TIME_ZONE`, the `EMBEDDINGS_*` group (see [Embeddings](#embeddings-hybrid-search)), the `LABELING_*` group (see [Auto-Labeling](#auto-labeling)), `ANALYSIS_STALLED_WORKER_GRACE_SECONDS` and `ANALYSIS_SWEEP_CRON` (see [Background Workers](#background-workers-and-crash-recovery)), `OTEL_EXPORTER_OTLP_ENDPOINT`, and `HTTP_PROXY`/`HTTPS_PROXY`/`NO_PROXY` behind a proxy.

!!! warning "No quotes in .env"
    Values must not be wrapped in quotes; Docker Swarm treats them as part of the value, and `stack-deploy` refuses to run when it finds any.

### Email

RADIS sends mail for finished jobs, account registrations awaiting approval, subscription notifications, and critical server errors:

- `DJANGO_EMAIL_URL`: SMTP server as a URL, e.g. `smtp://user:password@mail.example.com:587?tls=True`. Only used in production; in development mails are printed to the `web` log instead
- `DJANGO_SERVER_EMAIL`: Sender address of all mail
- `DJANGO_ADMIN_EMAIL`, `DJANGO_ADMIN_FULL_NAME`: Receive critical error reports and the "account awaiting approval" mails
- `SUPPORT_EMAIL`: Shown to users as the address to contact for help

Subjects are prefixed with `[RADIS]`. To check the settings, run the `send_test_mail` management command (optionally with a recipient address; it defaults to `DJANGO_ADMIN_EMAIL`).

### Running Management Commands

Some sections below ask you to run a management command (`embed_pending`, `labels_status`, `sweep_stale_tasks`, `send_test_mail`). They run inside the `web` container. In production the stack runs under Docker Swarm, so find the container first:

```terminal
docker exec -it $(docker ps -q -f name=radis_prod_web | head -n1) ./manage.py labels_status
```

`radis_prod` is the stack name (`STACK_NAME`, default `radis_prod`). In development `docker compose exec web ./manage.py labels_status` does the same.

## Updating RADIS

Follow these steps to safely update your RADIS installation:

1. **Verify no active jobs**
2. **Enable maintenance mode**: In Django Admin, navigate to **Common** → **Project settings**, check "Maintenance" and save
3. **Navigate to the production folder** (e.g. `radis_prod`, or whatever you named it)
4. **Backup database**: Run `uv run cli db-backup` to create a database backup (see [Backups](../backups.md))
5. **Remove stack**: Run `uv run cli stack-rm` to remove all Docker containers and services
6. **Pull latest changes**: Run `git pull origin main` to fetch the latest code updates
7. **Update environment**: Compare `example.env` with your `.env` file and add any new environment variables or update changed values. Pay particular attention to the `LLM_*`, `EMBEDDINGS_*`, `LABELING_*`, and `ANALYSIS_*` groups, which are explained below. Keep `STACK_NAME` unchanged, otherwise a second stack is deployed next to the old one
8. **Pull Docker images**: Run `uv run cli compose-pull` to download the latest Docker images
9. **Deploy stack**: Run `uv run cli stack-deploy` to start all services with the updated image
10. **Disable maintenance mode**: Uncheck "Maintenance" in **Project settings** and save

Depending on what changed, one of these follow-up steps may be needed after the stack is up:

- If you enabled or changed the embedding model, backfill the vectors of existing reports (see [Embeddings](#embeddings-hybrid-search))
- If you added or changed labels, run a labeling backfill (see [Auto-Labeling](#auto-labeling))

Tasks that were interrupted by the restart are repaired automatically (see [Background Workers](#background-workers-and-crash-recovery)).

## LLM and Embeddings Configuration

All AI features (chats, extractions, subscriptions, labeling, query generation) send their requests to a single OpenAI-compatible endpoint. RADIS ships no inference server; you point it at one you operate (e.g. Ollama, vLLM, SGLang, an LLM gateway) or at a hosted API. The endpoint must be reachable from inside the containers on every node of the stack; `host.docker.internal` only works in development.

### LLM Settings

Set these in `.env`:

- `LLM_BASE_URL` (required): Base URL of the endpoint, e.g. `http://llm.internal:11434/v1`
- `LLM_API_KEY`: API key for the endpoint. Optional; many self-hosted providers ignore it
- `LLM_DEFAULT_MODEL` (required): The model every feature uses unless overridden
- `LLM_CHATS_MODEL`, `LLM_QUERY_GENERATION_MODEL`, `LLM_EXTRACTIONS_MODEL`, `LLM_SUBSCRIPTIONS_MODEL`, `LLM_LABELING_MODEL`: Per-feature overrides. Leave blank to use the default model. Useful to spend a stronger model where it matters (e.g. labeling) and a fast one for chat

Every model setting takes the form `model[?param=value&...]`. The parameters are merged into each request body, so both standard fields and provider extensions work:

```text
LLM_DEFAULT_MODEL=qwen3:8b?temperature=0&chat_template_kwargs.enable_thinking=false
```

Values are read as JSON where possible (`temperature=0` sends a number, `reasoning_effort=none` sends the string `"none"`), and a dotted key becomes a nested object. Drop parameters the provider does not know if it rejects requests with a 400 error.

Request timeout, rate-limit handling, and transient retries have sensible defaults (`LLM_REQUEST_TIMEOUT_SECONDS`, `LLM_RATE_LIMIT_*`, `LLM_TRANSIENT_RETRY_*`); override them only if needed.

### Embeddings (Hybrid Search)

By default RADIS runs full-text search only. Setting `EMBEDDINGS_MODEL` switches on hybrid search, where full-text and semantic (vector) results are fused into one ranking:

- `EMBEDDINGS_MODEL`: The embedding model, in the same `model[?param=value]` form. Leave empty for full-text search only
- `EMBEDDINGS_BASE_URL`, `EMBEDDINGS_API_KEY`: Default to `LLM_BASE_URL` and `LLM_API_KEY`. Set them only when embeddings are served from a different endpoint, which is the normal case for vLLM and SGLang since they serve one model per process
- `EMBEDDINGS_DIM`: Vector width (default `1024`). This is coupled to the database schema: changing it after deployment requires dropping the embedding column, re-migrating, and re-embedding all reports. If the model spec also sets `dimensions`, both must agree; RADIS checks this at startup
- `EMBEDDINGS_QUERY_INSTRUCTION`: Model-specific instruction prefix for search queries (some models, e.g. Qwen3-Embedding, want one)
- `EMBEDDINGS_QUERY_CACHE_TIMEOUT_SECONDS`: How long a query's vector stays cached (default 900 s). Lower it temporarily after swapping the model, or the old vectors keep serving stale results for up to this long

New and updated reports are embedded automatically by the `embeddings_worker` service. Reports that existed before the model was configured have no vector and are found by full-text search only until you backfill them with the `embed_pending` management command (see [Running Management Commands](#running-management-commands)). It is idempotent and resumable, refuses to start while another backfill is running or `EMBEDDINGS_MODEL` is empty, and takes `--limit` and `--subjob-size` options; `embed_cancel` stops a running backfill.

The same is available in Django Admin under **Pgsearch** → **Report search indexes**: the page shows the state of the embedding pipeline, has a **Cancel queued backfill** button, and offers the actions "Enqueue embedding for selected rows (NULL only)" and "Clear embeddings (NULL them)" on selected reports. **Embedding backfill runs** lists past and current backfills.

When the embedding service is unreachable or rate limiting, searches silently fall back to full-text results and a warning is logged in the `web` service.

See the [Contributing guide](../dev-docs/contributing.md) for how to run Ollama with a chat and an embedding model, and the troubleshooting sections there for connection problems.

## User and Group Management

Administrators can create users by navigating to the Django Admin section. Alternatively, users can self-register, after which an administrator must approve and activate their account.

RADIS uses a group-based permission system:

- **Groups** define access to specific reports based on organizational structure
- **Users** are assigned to one or more groups to inherit their permissions
- **Active Group** determines which reports a user can currently access

### Creating and Managing Groups

#### Access Django Admin

- Log in as a staff user
- Go to **Admin Section** → **Django Admin** (available at `/django-admin/` URL path)

#### Create/Edit Groups

- Navigate to **Authentication and Authorization** → **Groups**
- Click "Add Group" or edit an existing group
- Give the group a **Name** (e.g., "Radiology Department", "Research Team", "Oncology")

#### Assign Permissions

- In the group form, you'll see **Available permissions** and **Chosen permissions**
- Select the permissions you want from the available list
- Move them to **Chosen permissions**

#### Add Users to Group

- In the **Users** section, select users from **Available users**
- Move them to **Chosen users**
- Click **Save** to apply all changes

### Active Group

Each user has an **active group** that determines which reports they can currently access:

- Only reports associated with the active group are visible in searches and report views. Collections are per user and not filtered by group: a report bookmarked while one group was active stays visible in the collection after switching to another
- This ensures proper data isolation between different departments or projects
- Users need an active group to create subscriptions and extraction jobs; the job or subscription is bound to that group

## Report Management

### Report Import and Management

Administrators can import reports programmatically via the RADIS API or using the RADIS Client library. The reports API only accepts tokens of staff users. See the **RADIS Client** section below for details.

Every import or update of a report re-indexes it for search, queues it for embedding (if configured), and marks its labels for re-labeling on the next scan (see below). This also applies to updates that do not change the report text, so avoid re-pushing an unchanged corpus if you want to keep LLM load down.

## Auto-Labeling

RADIS can label reports automatically with an LLM. Labels are defined system-wide by administrators in Django Admin; there is no in-app editor. Users see the results as badges on the report detail page and can filter searches by label.

### Concepts

- A **Label group** has a name and a **gate question**: a Yes/No question the LLM answers once per report before any label of the group is evaluated (e.g. "Is this a CT of the chest?"). A No answer skips the whole group for that report, which is the main lever for keeping LLM cost down
- A **Label** belongs to one group and has a name (unique across all groups), a **description** that defines the finding for the LLM, and an **active** flag. Inactive labels are neither classified nor offered in the search filter
- For each report and active label the LLM assigns one of five results: Present, Likely, Possible, Absent, or Unmentioned. Only the first three surface as badges and in the search filter

### Managing Labels

1. **Access Django Admin**: Navigate to **Labels** → **Label groups** and create a group with its gate question
2. Navigate to **Labels** → **Labels** and add labels to the group. Write the description as the definition you would give a radiologist; it is sent to the LLM verbatim
3. Run a backfill (see below) so that existing reports are labeled

Saving a label (any field, not only the description) makes its existing results **stale**; saving a group (e.g. its gate question) makes the group's gate answers stale, but not the results of its labels. Stale results keep surfacing (with a greyed-out badge) until a backfill regenerates them; the periodic scan only picks up changed reports, not changed labels. Deactivating a label stops new classifications, but results already assigned remain visible on the reports.

### Backfill and Periodic Scan

Labeling runs as background jobs on the `llm` queue:

- **Periodic scan**: Every night (`LABELING_SCAN_CRON`, default `0 2 * * *`) RADIS labels all reports that were created or updated since the last scan. The first scan after installation only records a checkpoint; reports that existed before are not labeled until you run a backfill
- **Backfill**: In Django Admin, navigate to **Labels** → **Labeling jobs** and click **Run backfill now**. This labels every report with a missing or stale result for any active label. The job is safe to cancel and restart. Only one labeling job can be active at a time; the scan skips its tick while a backfill runs

The **Labeling jobs** page shows all jobs with their trigger (Periodic scan or Manual backfill) and status; a job can be canceled from its detail page. **Label results**, **Gate answers**, and **Labeling tasks** are available read-only for inspection. A task ends with status Warning when some of its reports could not be labeled and Failure when none could, which usually points to an LLM outage.

To check the corpus-wide state (checkpoint, per-label and per-gate counts, stale counts), run the `labels_status` management command (see [Running Management Commands](#running-management-commands)). The checkpoint alone is also visible in Django Admin under **Labels** → **Labeling scan checkpoints**.

Tuning variables in `.env`: `LABELING_TASK_BATCH_SIZE`, `LABELING_LLM_CONCURRENCY_LIMIT`, `LABELING_GATE_BATCH_SIZE`, `LABELING_JOB_PRIORITY`, and the optional prompt overrides `LABELING_SYSTEM_PROMPT` and `LABELING_GATE_SYSTEM_PROMPT`. Use `LLM_LABELING_MODEL` to run labeling on a different model than the other features.

## Extraction Jobs

Users create extraction jobs through a wizard (see the [User Guide](user-guide.md#6-extractions)). Administrators verify and monitor them.

### Verifying Jobs

Jobs created by regular users start in the **Unverified** state and are not processed until a staff user opens the job page and clicks **Verify Job**. Jobs created by staff users are queued immediately. Staff users see all users' jobs by appending `?all=1` to the job list URL.

### Managing Extractions

1. **Access Django Admin**: Navigate to **Extractions** → **Extraction jobs**
2. **View Details**: Click on a job to see its owner and group, query and filters, status and message, and its output fields
3. **Extraction tasks** show the individual batches of a job together with the extracted data per report

Each job may cover at most 25,000 reports; the wizard refuses larger result sets.

### Urgent Priority

A job whose **urgent** flag is set is queued ahead of regular jobs. The flag can only be set in Django Admin, and only takes effect on jobs that are not queued yet (e.g. still Unverified), because the priority is fixed when the job is queued. The permission `Can analyze urgently` that exists on subscription jobs is not checked anywhere.

## Subscription Management

Subscriptions are refreshed at the top of every hour. Each refresh looks at the reports created or updated since the previous refresh, applies the subscription's filters, and — if the subscription has filter questions or extraction fields — sends each report to the LLM on the `llm` queue. Users need the permission `subscriptions | subscription | Can add subscription` and an active group to create subscriptions.

### Managing Subscriptions

1. **Access Django Admin**: Navigate to **Subscriptions** → **Subscriptions**
2. **View All Subscriptions**: Click on a subscription entry to view its full details, including its owner, group, and "last refreshed" timestamp. Changing "last refreshed" changes which reports the next refresh treats as new
3. **Delete a Subscription**: Subscriptions can be removed if necessary using the Delete action. This also deletes the inbox items collected so far

The subscription list only shows a user's own subscriptions. Staff users can still open any user's inbox and its CSV export by URL (the subscription ID is shown in Django Admin).

A refresh only considers reports of the owner's **current** active group, not the group the subscription was created in. A subscription silently stops collecting reports while its owner has another group active.

## Background Workers and Crash Recovery

Extraction, subscription, and labeling jobs are processed by the `default_worker` and `llm_worker` services; embeddings by `embeddings_worker`. Check their logs (`docker compose logs llm_worker`) when jobs do not progress.

When a worker container is killed mid-task (crash, out-of-memory, redeploy), the affected tasks are repaired automatically: `default_worker` and `llm_worker` sweep stale tasks on startup, and a periodic sweep (`ANALYSIS_SWEEP_CRON`, default every minute) requeues tasks whose worker has been silent for longer than `ANALYSIS_STALLED_WORKER_GRACE_SECONDS` (default 30; never set it lower). Embedding subjobs are not analysis tasks; `embeddings_worker` runs no sweep, and interrupted subjobs are retried by the task queue itself. On every deploy the `init` service additionally runs `retry_stalled_jobs`, which re-enqueues queue jobs left behind by dead workers. No manual step is needed after a deploy; to run the sweep by hand, run the `sweep_stale_tasks` management command (see [Running Management Commands](#running-management-commands)).

## System Announcements

System administrators can inform users about important updates, maintenance schedules, or system changes through the announcement feature.

### Creating Announcements

1. **Access Admin Interface**: Navigate to the Django admin interface (typically accessible at `/django-admin/`)
2. **Find Project Settings**: Go to the "Common" section and select "Project settings"
3. **Edit Announcement**: In the Project Settings form, locate the "Announcement" field
4. **Enter Message**: Type your announcement message. HTML formatting is supported for rich text display
5. **Save Changes**: Click "Save" to publish the announcement

### Announcement Display

- Announcements appear prominently on the main/home page
- Everyone who opens the home page sees the announcement, including visitors who are not logged in

#### Example Announcements

**Maintenance Notice:**

```html
<strong>Scheduled Maintenance:</strong> RADIS will be offline for maintenance on
<strong>March 15, 2024 from 2:00 AM to 4:00 AM UTC</strong>. Please plan your
extractions and subscriptions accordingly.
```

**New Feature Announcement:**

```html
<strong>New Feature Available:</strong> You can now create custom extraction
jobs with multiple output fields. Check out the user guide for more details.
```

## RADIS Client

RADIS Client is a Python library to create, retrieve, update and delete reports without using the web interface. The reports API is restricted to staff users, so the token must belong to a staff account.

### Creating API Tokens

To create an API token for programmatic access:

1. **Navigate** to **Token Authentication** by going to **"Profile"** --> **"Manage API Tokens"**
2. **Description** & **Expiry Time** : Add a description (optional) and choose an expiry time of 1, 7, 30 or 90 days. "Never" is only offered to users with the permission `token_authentication | token | Can generate never expiring token`.
3. **Click** on **"Generate Token"**.
4. This token will only be visible once, so make sure to copy it now and store it in a safe place. As you will not be able to see it again, you will have to generate a new token if you lose it.

### Revoking Tokens

- **Users** delete their own tokens on the same **Manage API Tokens** page
- **Admins** can revoke any token by navigating to **Django Admin** --> **Token Authentication**
