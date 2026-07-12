# Embedding Badge Per-Subjob Report Counts Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Show how many reports the queued and in-flight embedding subjobs cover in the admin pipeline badge, computed DB-side from each Procrastinate job's `args->'report_ids'`.

**Architecture:** Extend `_embedding_pipeline_stats()` in the `ReportSearchIndex` admin to aggregate, per job status, both the subjob count and the summed `jsonb_array_length(args -> 'report_ids')`. The badge template renders `2 subjobs queued (1000 reports) · 4 subjobs in-flight (2000 reports)`; a zero subjob count renders plain `0 queued` / `0 in-flight` with no parenthetical. The report-id arrays never leave Postgres.

**Tech Stack:** Django 5/6 ORM (`Func` + `KeyTransform` over JSONB), Django admin template override, pytest-django (transactional tests, `admin_client` fixture).

**Spec:** `docs/superpowers/specs/2026-07-12-embedding-badge-subjob-report-counts-design.md`

## Global Constraints

- Line length 100 for Python (ruff), 120 for templates (djlint).
- Tests must run against the transactional Postgres DB: postgres container must be up. Run locally with `FORCE_DEBUG_TOOLBAR=false uv run pytest ...` (the local `.env` forces the debug toolbar on, which breaks view tests under pytest's `DEBUG=False`).
- `ProcrastinateJob` is read-only via the ORM — tests insert rows with raw SQL through the existing `_insert_procrastinate_job` helper.
- `failed` stays a bare subjob count in the badge (no report parenthetical) — per spec.
- Commit after each task with the trailer `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`.

---

### Task 1: Per-status report sums in `_embedding_pipeline_stats`

**Files:**
- Modify: `radis/pgsearch/admin.py` (imports at top; `_embedding_pipeline_stats` at ~line 90)
- Test: `radis/pgsearch/tests/test_admin.py`

**Interfaces:**
- Consumes: `ProcrastinateJob.args` JSONB of shape `{"report_ids": [int, ...]}` (written by `enqueue_embed_reports` in `radis/pgsearch/tasks.py`).
- Produces: `_embedding_pipeline_stats()` returns dict with keys `pending_reports`, `todo`, `todo_reports`, `todo_backfill`, `doing`, `doing_reports`, `failed` (all `int`, report sums default 0). Task 2's template consumes `todo_reports` / `doing_reports`.

- [ ] **Step 1: Extend the SQL test helper to take report ids**

In `radis/pgsearch/tests/test_admin.py`, replace `_insert_procrastinate_job` with:

```python
def _insert_procrastinate_job(
    status: str,
    queue: str = "embeddings",
    priority: int = 0,
    report_ids: list[int] | None = None,
    args_json: str | None = None,
) -> None:
    """Insert a row directly via SQL because ProcrastinateJob's Django ORM
    surface is intentionally read-only — Procrastinate owns writes. The
    stats helper reads (queue_name, status, priority) and sums
    jsonb_array_length(args->'report_ids'); `args_json` overrides the args
    payload entirely (e.g. '{}' for a job without report_ids)."""
    if args_json is None:
        args_json = json.dumps({"report_ids": report_ids or []})
    with connection.cursor() as cur:
        cur.execute(
            "INSERT INTO procrastinate_jobs "
            "(queue_name, task_name, priority, lock, queueing_lock, args, status, attempts) "
            "VALUES (%s, %s, %s, NULL, NULL, %s, %s::procrastinate_job_status, %s)",
            [
                queue,
                "radis.pgsearch.tasks.embed_reports_task",
                priority,
                args_json,
                status,
                0,
            ],
        )
```

Add `import json` to the imports at the top of the file.

- [ ] **Step 2: Write the failing tests**

Add to `radis/pgsearch/tests/test_admin.py` (after `test_pipeline_stats_counts_procrastinate_jobs_by_status`):

```python
def test_pipeline_stats_sums_reports_per_status():
    _insert_procrastinate_job("todo", report_ids=[1, 2])
    _insert_procrastinate_job("todo", report_ids=[3, 4, 5])
    _insert_procrastinate_job("doing", report_ids=[6, 7, 8, 9])
    # Failed jobs stay a bare subjob count — their reports are not summed
    # into either report total.
    _insert_procrastinate_job("failed", report_ids=[10])
    # Job on a different queue must not be counted.
    _insert_procrastinate_job("todo", queue="default", report_ids=[11, 12])

    stats = ReportSearchIndexAdmin._embedding_pipeline_stats()
    assert stats["todo"] == 2
    assert stats["todo_reports"] == 5
    assert stats["doing"] == 1
    assert stats["doing_reports"] == 4
    assert stats["failed"] == 1


def test_pipeline_stats_tolerates_jobs_without_report_ids():
    """A job whose args lack report_ids contributes NULL to the sum, which
    Sum() skips — the subjob is still counted, the report total isn't."""
    _insert_procrastinate_job("todo", report_ids=[1, 2, 3])
    _insert_procrastinate_job("todo", args_json="{}")

    stats = ReportSearchIndexAdmin._embedding_pipeline_stats()
    assert stats["todo"] == 2
    assert stats["todo_reports"] == 3
```

Also update the exact-equality zero test to include the new keys:

```python
def test_pipeline_stats_zero_when_no_queue_activity():
    stats = ReportSearchIndexAdmin._embedding_pipeline_stats()
    assert stats == {
        "pending_reports": 0,
        "todo": 0,
        "todo_reports": 0,
        "todo_backfill": 0,
        "doing": 0,
        "doing_reports": 0,
        "failed": 0,
    }
```

- [ ] **Step 3: Run tests to verify the new ones fail**

Run: `FORCE_DEBUG_TOOLBAR=false uv run pytest radis/pgsearch/tests/test_admin.py -q`
Expected: `test_pipeline_stats_sums_reports_per_status`, `test_pipeline_stats_tolerates_jobs_without_report_ids`, and `test_pipeline_stats_zero_when_no_queue_activity` FAIL with `KeyError: 'todo_reports'` / dict mismatch. All other tests PASS.

- [ ] **Step 4: Implement the aggregation**

In `radis/pgsearch/admin.py`, extend the `django.db.models` imports:

```python
from django.db.models import Count, Func, IntegerField, Sum
from django.db.models.fields.json import KeyTransform
```

(keep the existing `from django.db.models import Count` line merged into this; `from django.db.models.query import QuerySet` stays as is.)

Replace `_embedding_pipeline_stats` with:

```python
    @staticmethod
    def _embedding_pipeline_stats() -> dict[str, int]:
        """Snapshot of the embedding pipeline for the admin badge: how many
        reports are still missing an embedding, and what Procrastinate is
        doing about it right now — subjob counts per status, plus how many
        reports the queued and in-flight subjobs cover. The report totals
        are summed DB-side from each job's args->'report_ids'
        (jsonb_array_length); the id arrays never leave Postgres, which
        matters when a large backfill holds millions of ids in `todo` jobs."""
        pending = ReportSearchIndex.objects.filter(embedding__isnull=True).count()
        report_count = Func(
            KeyTransform("report_ids", "args"),
            function="jsonb_array_length",
            output_field=IntegerField(),
        )
        queue_rows = {
            row["status"]: row
            for row in ProcrastinateJob.objects.filter(queue_name="embeddings")
            .values("status")
            .annotate(jobs=Count("id"), reports=Sum(report_count))
        }
        # Counted separately because the cancel-backfill button only cancels
        # backfill-priority jobs — gating it on the overall todo count would
        # offer a cancel that then reports "nothing to cancel" whenever the
        # queue holds only live write-path jobs.
        todo_backfill = ProcrastinateJob.objects.filter(
            queue_name="embeddings",
            status="todo",
            priority=settings.EMBEDDING_BACKFILL_PRIORITY,
        ).count()
        todo_row = queue_rows.get("todo", {})
        doing_row = queue_rows.get("doing", {})
        return {
            "pending_reports": pending,
            "todo": todo_row.get("jobs", 0),
            # Sum() returns NULL when no job has report_ids — coalesce here.
            "todo_reports": todo_row.get("reports") or 0,
            "todo_backfill": todo_backfill,
            "doing": doing_row.get("jobs", 0),
            "doing_reports": doing_row.get("reports") or 0,
            "failed": queue_rows.get("failed", {}).get("jobs", 0),
        }
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `FORCE_DEBUG_TOOLBAR=false uv run pytest radis/pgsearch/tests/test_admin.py -q`
Expected: all tests PASS (including the pre-existing ones — `todo`, `doing`, `failed`, `todo_backfill`, `pending_reports` semantics are unchanged).

- [ ] **Step 6: Commit**

```bash
git add radis/pgsearch/admin.py radis/pgsearch/tests/test_admin.py
git commit -m "Sum per-subjob report counts in embedding pipeline stats

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 2: Render report totals in the badge template

**Files:**
- Modify: `radis/pgsearch/templates/admin/pgsearch/reportsearchindex/change_list.html`
- Test: `radis/pgsearch/tests/test_admin.py`

**Interfaces:**
- Consumes: `embedding_pipeline_stats` context dict from Task 1 — keys `todo`, `todo_reports`, `doing`, `doing_reports` (ints).
- Produces: badge HTML. Squashed to single-space whitespace it reads: `2 subjobs queued (5 reports)` / `1 subjob in-flight (4 reports)`; zero counts read `0 queued` / `0 in-flight` with no parenthetical.

- [ ] **Step 1: Write the failing rendering tests**

Add to `radis/pgsearch/tests/test_admin.py` (imports: add `import re` and `from django.urls import reverse` at the top; the file currently imports `reverse` locally inside two tests — a top-level import is fine alongside those):

```python
def _squash_ws(html: str) -> str:
    """Template newlines/indentation render as whitespace runs; collapse
    them so assertions can match across djlint's line wrapping."""
    return re.sub(r"\s+", " ", html)


def test_changelist_badge_shows_subjob_report_counts(admin_client):
    _insert_procrastinate_job("todo", report_ids=[1, 2])
    _insert_procrastinate_job("todo", report_ids=[3, 4, 5])
    _insert_procrastinate_job("doing", report_ids=[6, 7, 8, 9])

    url = reverse("admin:pgsearch_reportsearchindex_changelist")
    html = _squash_ws(admin_client.get(url).content.decode())

    assert "<strong>2</strong> subjobs queued (5 reports)" in html
    assert "<strong>1</strong> subjob in-flight (4 reports)" in html


def test_changelist_badge_zero_counts_render_plain(admin_client):
    url = reverse("admin:pgsearch_reportsearchindex_changelist")
    html = _squash_ws(admin_client.get(url).content.decode())

    assert "<strong>0</strong> queued" in html
    assert "<strong>0</strong> in-flight" in html
    assert "subjob" not in html
    assert "reports)" not in html
```

Note: `admin_client` is pytest-django's built-in logged-in-superuser client. If it errors on the project's custom user model, replace it with a fixture that creates a superuser via `adit_radis_shared.accounts.factories.AdminUserFactory` (check `radis/reports/tests/test_api.py:75` for the existing pattern) and `client.force_login(user)`.

- [ ] **Step 2: Run tests to verify they fail**

Run: `FORCE_DEBUG_TOOLBAR=false uv run pytest radis/pgsearch/tests/test_admin.py -q -k changelist_badge`
Expected: both FAIL — the current template renders `2 queued`, never the word "subjob".

- [ ] **Step 3: Update the template**

In `radis/pgsearch/templates/admin/pgsearch/reportsearchindex/change_list.html`, replace these two lines:

```django
            &nbsp;·&nbsp; <strong>{{ embedding_pipeline_stats.todo }}</strong> queued
            &nbsp;·&nbsp; <strong>{{ embedding_pipeline_stats.doing }}</strong> in-flight
```

with:

```django
            &nbsp;·&nbsp;
            {% if embedding_pipeline_stats.todo %}
                <strong>{{ embedding_pipeline_stats.todo }}</strong> subjob{{ embedding_pipeline_stats.todo|pluralize }} queued
                ({{ embedding_pipeline_stats.todo_reports }} report{{ embedding_pipeline_stats.todo_reports|pluralize }})
            {% else %}
                <strong>0</strong> queued
            {% endif %}
            &nbsp;·&nbsp;
            {% if embedding_pipeline_stats.doing %}
                <strong>{{ embedding_pipeline_stats.doing }}</strong> subjob{{ embedding_pipeline_stats.doing|pluralize }} in-flight
                ({{ embedding_pipeline_stats.doing_reports }} report{{ embedding_pipeline_stats.doing_reports|pluralize }})
            {% else %}
                <strong>0</strong> in-flight
            {% endif %}
```

The `failed` segment and everything else stays untouched.

- [ ] **Step 4: Run tests to verify they pass**

Run: `FORCE_DEBUG_TOOLBAR=false uv run pytest radis/pgsearch/tests/test_admin.py -q`
Expected: all PASS.

- [ ] **Step 5: Lint**

Run: `uv run cli lint`
Expected: ruff and djlint pass. If djlint reformats the template, re-run the two badge tests (the `_squash_ws` helper makes them robust to rewrapping) and include the reformatted file in the commit.

- [ ] **Step 6: Commit**

```bash
git add radis/pgsearch/templates/admin/pgsearch/reportsearchindex/change_list.html radis/pgsearch/tests/test_admin.py
git commit -m "Show per-subjob report counts in embedding pipeline badge

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```
