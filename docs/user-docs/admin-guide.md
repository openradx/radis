# Admin Guide

The Admin Guide is intended for system administrators and technical staff responsible for configuring and maintaining RADIS (Radiology Report Archive and Discovery System).

## Installation

```terminal
Clone the repository: git clone https://github.com/openradx/radis.git
cd radis
uv sync
cp ./example.env ./.env  # copy example environment to .env
uv run cli stack-deploy  # builds and starts Docker containers
```

## Updating RADIS

Follow these steps to safely update your RADIS installation:

1. **Verify no active jobs**
2. **Enable maintenance mode**: In Django Admin, navigate to **Common** → **Project Settings** and check the "Maintenance mode" checkbox, then save
3. Navigate to Production folder
4. **Backup database**: Run `uv run cli db-backup` to create a database backup
5. **Remove stack**: Run `uv run cli stack-rm` to remove all Docker containers and services
6. **Pull latest changes**: Run `git pull origin main` to fetch the latest code updates
7. **Update environment**: Compare `example.env` with your `.env` file and add any new environment variables or update changed values
8. **Pull Docker images**: Run `uv run cli compose-pull` to download the latest Docker images
9. **Deploy stack**: Run `uv run cli stack-deploy` to rebuild and start all services with the updated code
10. **Disable maintenance mode**: In Django Admin, navigate to **Common** → **Project Settings** and uncheck the "Maintenance mode" checkbox, then save

Step 9 can take much longer than usual when a release ships a data migration.
The `init` service runs `manage.py migrate` and every other service waits for it
to finish, so the stack stays unavailable for the whole migration. The release
that added the search projection to the report search index is the current
example: it rewrites every row of that table, measured at about **ten minutes
for 8 million reports** and proportionally longer on a larger archive. Plan the
maintenance window around that, and do not shorten `WAIT_INIT_TIMEOUT` (default
one hour, see the compose files) below the expected migration time -- the
services that wait on `init` exit when that timeout expires.

Keep the web tier stopped for the whole migration, which steps 5 and 9 above
already do -- do not start it early to "check on progress". While that
particular migration is between adding the projection columns and backfilling
them, every row carries an empty group list. A search that runs with no active
group (the extraction preview does this for a user without one) filters on
exactly that empty list, so during the window it would match the whole archive
instead of only the reports belonging to no group.

## Database tuning

Search scans the report index table in parallel, so the number of parallel
workers is the one PostgreSQL setting worth revisiting. These are optional
overrides -- set them in `.env` only if the defaults do not suit your hardware.

| Variable | Default | Guidance |
| --- | --- | --- |
| `POSTGRES_MAX_PARALLEL_WORKERS_PER_GATHER` | `4` | Measured on 5M reports: 606 ms at 2, 421 ms at 4, 343 ms at 8. Four captures most of the gain while leaving cores for concurrent searches. |
| `POSTGRES_MAX_PARALLEL_WORKERS` | `8` | PostgreSQL's default. Raise together with the two others on a larger host. |
| `POSTGRES_MAX_WORKER_PROCESSES` | `8` | As above. |
| `POSTGRES_SHARED_BUFFERS` | `128MB` | PostgreSQL's default. Around 25% of host RAM is the usual recommendation; a value larger than the container's memory will prevent PostgreSQL from starting. |

### Reclaiming space after the search-projection migration

The release that added the search projection rewrites every row of the report
index table, which roughly doubles it on disk -- measured at 8 million reports,
the table grew from 10 GB to 21 GB. Autovacuum reclaims the dead rows within
minutes and no action is needed to keep the database healthy, but reclaiming
them does not shrink the file: the space is marked reusable and stays allocated.

Search scans that table, so until the space is reused the scan reads about twice
the pages it needs to. How much that costs depends on whether the inflated table
still fits the host's page cache:

- **It fits.** Expect roughly the page-count ratio, near 1.9x on the scan. At
  8 million reports the inflated table is about 27 GB including indexes, so any
  host with 64 GB or more stays in this case.
- **It does not fit.** The scan starts reading from disk and the penalty grows
  well beyond 2x. On a 31 GB test host with about 20 GB of page cache, a query
  matching every report took 2,550 ms inflated against 870 ms compacted.

**Do not expect this to resolve on its own within any useful timeframe.** The
backfill leaves free space equal to roughly one full copy of the table, and only
*net new* reports consume it -- an update frees its own old row version, so
report edits are space-neutral. Refilling an 8-million-row archive therefore
takes on the order of 8 million new reports, which at typical hospital volumes is
many years. Emptied pages stay part of the table and are still read by every
scan.

Plan to compact the table once after this migration. The tool is
`VACUUM FULL pgsearch_reportsearchindex;`, and it needs a maintenance window.

**Do not bolt this onto the upgrade window.** Nothing is broken in the meantime
-- the system is fully functional, search is just slower -- so deploy the release
first, bring the stack back up, and schedule the compaction separately. That way
a long compaction never blocks a release.

**Budget the time realistically.** Copying the table is the quick part. The cost
is dominated by rebuilding the HNSW vector index, because `VACUUM FULL` relocates
every row and so invalidates and rebuilds every index on the table. Building an
HNSW graph over millions of vectors takes hours, and `VACUUM FULL` holds an
`ACCESS EXCLUSIVE` lock for the whole operation, so search is unavailable
throughout. With `EMBEDDINGS_MODEL` empty there is no HNSW index to rebuild and
the whole thing takes minutes instead (measured: 3 minutes for 8 million
reports).

Nothing is recomputed and nothing is lost. Embeddings are stored data and are
copied across as they are -- the embedding service is never called, and the index
is rebuilt from the vectors already in the table.

Before starting:

- **Free disk.** Both copies of the table exist at once, so the filesystem needs
  room for the compacted table on top of the inflated one.
- **Raise `maintenance_work_mem`** for the session. pgvector builds the HNSW
  graph in memory when it fits and falls back to a much slower on-disk path when
  it does not. At 8 million rows and 1024 dimensions the vectors alone are tens of
  gigabytes, so the default leaves you on the slow path. Set it as high as the
  host can spare, for example `SET maintenance_work_mem = '32GB';` in the same
  session as the `VACUUM FULL`.
- **Enable maintenance mode** so users get an explanation rather than timeouts.

If hours of unavailable search is not acceptable, there are two ways to shorten
it, both optional:

- **Rebuild the vector index separately.** Drop the HNSW index first, run
  `VACUUM FULL` (now minutes, since only small indexes are rebuilt), bring the
  stack back up, then recreate the index without blocking:

  ```sql
  DROP INDEX pgsearch_embedding_hnsw;
  VACUUM FULL pgsearch_reportsearchindex;
  -- bring the stack back up here, then:
  CREATE INDEX CONCURRENTLY pgsearch_embedding_hnsw
    ON pgsearch_reportsearchindex USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);
  ```

  This trades hours of full downtime for minutes of downtime plus hours of
  degraded semantic search: without the index, vector similarity falls back to an
  exact scan over every row, which is very slow on a large archive. Full-text
  search is unaffected. Keep the index name and definition exactly as above, or
  Django's migration state will no longer match the database. If
  `CREATE INDEX CONCURRENTLY` fails it leaves an invalid index behind -- drop it
  and retry.
- **Use [`pg_repack`](https://reorg.github.io/pg_repack/)**, which does the same
  compaction against a shadow copy while the table stays online, taking a brief
  exclusive lock only at the swap. It is not part of the PostgreSQL image RADIS
  ships, so this means building or installing the extension and its client binary
  first.

A plain `VACUUM` is not an alternative for any of this: it reclaims dead rows,
which autovacuum has already done, and leaves the file at its inflated size.

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

- Only reports associated with the active group are visible in searches and collections
- This ensures proper data isolation between different departments or projects

## Report Management

### Report Import and Management

Administrators can import reports programmatically via the RADIS API or using the RADIS Client library. See the **RADIS Client** section below for details.

### Extraction Jobs

Administrators can monitor and manage extraction jobs where AI analyzes reports to extract structured data.

### Managing Extractions

1. **Access Django Admin**: Navigate to **Extractions** → **Extraction Jobs**
2. **Monitor Status**: View jobs by status (Preparing, Pending, In Progress, Success, Failure, Canceled)
3. **View Details**: Click on a job to see:
   - Owner and group
   - Query and filters
   - Output field
   - Results data

### Granting Urgent Priority Permission

Users with urgent priority permission can skip the queue:

1. Navigate to **Authentication and Authorization** → **Groups**
2. Edit the desired group
3. Add permission: `extractions | extraction job | Can process urgently`
4. Save

## Subscription Management

Administrators can view and manage all user subscriptions through the Django Admin interface.

### Managing Subscriptions

1. **Access Django Admin**: Navigate to **Subscriptions** → **Subscriptions**
2. **View All Subscriptions**: Click on a subscription entry to view its full details.
3. **Delete a Subscription**: Subscriptions can be removed if necessary using the Delete action

### Urgent Priority Permission

1. Navigate to **Authentication and Authorization** → **Groups**
2. Edit the desired group
3. Add permission: `subscriptions | subscription | Can process urgently`
4. Save

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
- All logged-in users will see the announcement when they access RADIS

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

RADIS Client is a Python library for programmatic access to RADIS features without using the web interface.

### Creating API Tokens

To create an API token for programmatic access:

1. **Navigate** to **Token Authentication** by going to **"Profile"** --> **"Manage API Token"**
2. **Description** & **Expiry Time** : Add a description (optional) and expiry time for the token.
3. **Click** on **"Generate Token"**.
4. This token will only be visible once, so make sure to copy it now and store it in a safe place. As you will not be able to see it again, you will have to generate a new token if you lose it.

### Revoking Tokens

- **Admins** can revoke tokens by navigating to **Django Admin** --> **Token Authentication**
