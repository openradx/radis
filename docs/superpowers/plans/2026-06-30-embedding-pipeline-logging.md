# Embedding-Pipeline Logging Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add operator-visible logs to the write-path embedding pipeline (ingest → upsert → defer → embed → store), matching project logging conventions and surfacing service flakiness, payload-too-large bisects, lifecycle, and operator-initiated backfills.

**Architecture:** Each pipeline transition gets a log call at the appropriate level (INFO lifecycle, WARNING degradation, ERROR fatal/skippable), using `logger = logging.getLogger(__name__)`, lazy `%s` formatting, and function-prefixed messages. Stamina retries surface as WARNING via a global `on_retry` hook registered in `PgSearchConfig.ready()` and filtered to the embed callsite by `details.name`. No new settings, no read-path changes, no correlation IDs.

**Tech Stack:** Python 3.12, Django 5.1, stamina (retry), Procrastinate (task queue), pytest + pytest-django + caplog.

**Spec:** `docs/superpowers/specs/2026-06-30-embedding-pipeline-logging-design.md`

---

## File map

| File | Role |
|---|---|
| `radis/pgsearch/tasks.py` | All embed-pipeline task logs + `_truncate_ids` helper + `_log_stamina_retry` hook function |
| `radis/pgsearch/apps.py` | INFO at ingest entry (`_index_reports`); stamina hook registration in `ready()` |
| `radis/pgsearch/management/commands/embed_pending.py` | INFO at command invoke + done |
| `radis/pgsearch/admin.py` | INFO in both admin actions |
| `radis/pgsearch/tests/test_embed_reports_task.py` | New caplog-asserting tests + reusable fixture |
| `radis/pgsearch/tests/test_apps_checks.py` | Test for ingest-entry log + stamina hook registration |
| `radis/pgsearch/tests/test_embed_pending_command.py` | Test for command INFOs |
| `radis/pgsearch/tests/test_admin.py` | Test for admin-action INFOs |

## Reusable caplog pattern

The `radis` logger has `propagate=False` in `settings/base.py`, so pytest's `caplog` fixture does not see records emitted under it unless its handler is attached directly. The existing test `test_bisects_on_too_large_and_isolates_offender` does this inline. We extract that into a fixture so the new tests are concise. Defined once in `test_embed_reports_task.py` (Task 2) and re-used by other test files via plain helper-function imports.

---

### Task 1: Reset partial edits to baseline

**Files:**
- Modify: `radis/pgsearch/tasks.py` (revert to HEAD)

Earlier exploratory edits added five logs to `tasks.py` that partially diverge from this spec (duration field missing on the finish line, wording on the catch ERROR differs). To make the TDD tasks below clean, restore the file to HEAD and rebuild via the tasks. The disposition table in the spec documents what's being thrown away.

- [ ] **Step 1: Confirm what will be discarded**

Run: `git diff radis/pgsearch/tasks.py`
Expected: shows five log additions in `enqueue_embed_reports`, `_embed_with_bisect`, and `embed_reports_task`.

- [ ] **Step 2: Reset the file**

Run: `git restore radis/pgsearch/tasks.py`

- [ ] **Step 3: Verify baseline**

Run: `git diff radis/pgsearch/tasks.py`
Expected: no output (file matches HEAD).

- [ ] **Step 4: No commit** — nothing changed against HEAD; move to Task 2.

---

### Task 2: Add caplog fixture for the `radis.pgsearch.tasks` logger

**Files:**
- Modify: `radis/pgsearch/tests/test_embed_reports_task.py`

The new tests in subsequent tasks all need caplog records from `radis.pgsearch.tasks`. Extract the attach-handler pattern into a fixture so we don't repeat the `addHandler` / `removeHandler` boilerplate.

- [ ] **Step 1: Add the fixture near the top of the file**

In `radis/pgsearch/tests/test_embed_reports_task.py`, after the `stamina_active` fixture, add:

```python
@pytest.fixture
def caplog_tasks(caplog):
    """Attach caplog's handler to `radis.pgsearch.tasks` directly.

    The `radis` logger has `propagate=False` in settings, so caplog's
    root handler doesn't see records emitted under it. Yield caplog
    so tests can assert on `caplog.records`."""
    task_logger = logging.getLogger("radis.pgsearch.tasks")
    task_logger.addHandler(caplog.handler)
    caplog.set_level(logging.DEBUG, logger="radis.pgsearch.tasks")
    try:
        yield caplog
    finally:
        task_logger.removeHandler(caplog.handler)
```

- [ ] **Step 2: Verify the existing log-assertion test still passes**

Run: `uv run cli test -- radis/pgsearch/tests/test_embed_reports_task.py::test_bisects_on_too_large_and_isolates_offender -v`
Expected: PASS.

- [ ] **Step 3: Commit**

```bash
git add radis/pgsearch/tests/test_embed_reports_task.py
git commit -m "test(pgsearch): add caplog_tasks fixture for embedding task logs"
```

---

### Task 3: Add `_truncate_ids` helper

**Files:**
- Modify: `radis/pgsearch/tasks.py` (add helper near module top)
- Modify: `radis/pgsearch/tests/test_embed_reports_task.py` (add unit test)

- [ ] **Step 1: Write the failing test**

In `radis/pgsearch/tests/test_embed_reports_task.py`, add at the bottom:

```python
def test_truncate_ids_returns_first_n():
    from radis.pgsearch.tasks import _truncate_ids

    assert _truncate_ids([1, 2, 3], limit=50) == [1, 2, 3]
    assert _truncate_ids(list(range(100)), limit=3) == [0, 1, 2]
    assert _truncate_ids([], limit=10) == []
```

- [ ] **Step 2: Run the failing test**

Run: `uv run cli test -- radis/pgsearch/tests/test_embed_reports_task.py::test_truncate_ids_returns_first_n -v`
Expected: FAIL with `ImportError: cannot import name '_truncate_ids'`.

- [ ] **Step 3: Implement the helper**

In `radis/pgsearch/tasks.py`, after `logger = logging.getLogger(__name__)`, add:

```python
def _truncate_ids(ids: list[int], limit: int = 50) -> list[int]:
    return list(ids[:limit])
```

- [ ] **Step 4: Run the test**

Run: `uv run cli test -- radis/pgsearch/tests/test_embed_reports_task.py::test_truncate_ids_returns_first_n -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add radis/pgsearch/tasks.py radis/pgsearch/tests/test_embed_reports_task.py
git commit -m "feat(pgsearch): add _truncate_ids helper for bounded ID-list logs"
```

---

### Task 4: Add INFO start log to `embed_reports_task`

**Files:**
- Modify: `radis/pgsearch/tasks.py` (in `embed_reports_task`)
- Modify: `radis/pgsearch/tests/test_embed_reports_task.py` (new test)

- [ ] **Step 1: Write the failing test**

In `radis/pgsearch/tests/test_embed_reports_task.py`, add:

```python
def test_logs_info_start_with_report_count(settings, caplog_tasks):
    reports = [ReportFactory.create() for _ in range(2)]
    pks = [r.pk for r in reports]
    vec = _unit_vec(settings.EMBEDDING_DIM)
    fake = _make_fake_client(vec)

    with patch("radis.pgsearch.tasks.EmbeddingClient", return_value=fake):
        embed_reports_task(report_ids=pks)

    info_msgs = [r.getMessage() for r in caplog_tasks.records if r.levelname == "INFO"]
    assert any("embed_reports_task: start; reports=2" in m for m in info_msgs)
```

Add `pytestmark = pytest.mark.django_db(transaction=True)` already covers it.

- [ ] **Step 2: Run the failing test**

Run: `uv run cli test -- radis/pgsearch/tests/test_embed_reports_task.py::test_logs_info_start_with_report_count -v`
Expected: FAIL — no INFO record with that text.

- [ ] **Step 3: Implement**

In `radis/pgsearch/tasks.py`, in `embed_reports_task`, replace:

```python
    if not report_ids:
        return

    rsvs = list(
```

with:

```python
    if not report_ids:
        return

    logger.info("embed_reports_task: start; reports=%d", len(report_ids))

    rsvs = list(
```

- [ ] **Step 4: Run the test**

Run: `uv run cli test -- radis/pgsearch/tests/test_embed_reports_task.py::test_logs_info_start_with_report_count -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add radis/pgsearch/tasks.py radis/pgsearch/tests/test_embed_reports_task.py
git commit -m "feat(pgsearch): log INFO at embed_reports_task start"
```

---

### Task 5: Add INFO finish log with `duration_ms`

**Files:**
- Modify: `radis/pgsearch/tasks.py` (add `import time`, capture `start_t`, log at end)
- Modify: `radis/pgsearch/tests/test_embed_reports_task.py` (new test)

- [ ] **Step 1: Write the failing test**

```python
def test_logs_info_finish_with_counts_and_duration(settings, caplog_tasks):
    reports = [ReportFactory.create() for _ in range(2)]
    pks = [r.pk for r in reports]
    vec = _unit_vec(settings.EMBEDDING_DIM)
    fake = _make_fake_client(vec)

    with patch("radis.pgsearch.tasks.EmbeddingClient", return_value=fake):
        embed_reports_task(report_ids=pks)

    info_msgs = [r.getMessage() for r in caplog_tasks.records if r.levelname == "INFO"]
    finish = [m for m in info_msgs if "embed_reports_task: finished" in m]
    assert finish, info_msgs
    assert "embedded=2" in finish[0]
    assert "skipped=0" in finish[0]
    assert "duration_ms=" in finish[0]
```

- [ ] **Step 2: Run the failing test**

Run: `uv run cli test -- radis/pgsearch/tests/test_embed_reports_task.py::test_logs_info_finish_with_counts_and_duration -v`
Expected: FAIL — no finish record.

- [ ] **Step 3: Implement**

In `radis/pgsearch/tasks.py`, change the top:

```python
import logging
```

to:

```python
import logging
import time
```

Then in `embed_reports_task`, change:

```python
    logger.info("embed_reports_task: start; reports=%d", len(report_ids))

    rsvs = list(
```

to:

```python
    logger.info("embed_reports_task: start; reports=%d", len(report_ids))
    start_t = time.perf_counter()

    rsvs = list(
```

And at the very end of the function body (after the existing `if skipped:` block), add:

```python
    duration_ms = int((time.perf_counter() - start_t) * 1000)
    logger.info(
        "embed_reports_task: finished; embedded=%d skipped=%d duration_ms=%d",
        len(embedded),
        len(skipped),
        duration_ms,
    )
```

- [ ] **Step 4: Run the test**

Run: `uv run cli test -- radis/pgsearch/tests/test_embed_reports_task.py::test_logs_info_finish_with_counts_and_duration -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add radis/pgsearch/tasks.py radis/pgsearch/tests/test_embed_reports_task.py
git commit -m "feat(pgsearch): log INFO at embed_reports_task finish with duration_ms"
```

---

### Task 6: Add ERROR on `EmbeddingClientError` exhaustion (catch + re-raise)

**Files:**
- Modify: `radis/pgsearch/tasks.py` (wrap embedding loop in try/except)
- Modify: `radis/pgsearch/tests/test_embed_reports_task.py` (new test)

The existing `test_embedding_error_propagates` already verifies the exception escapes; we extend with an assertion on the new ERROR log.

- [ ] **Step 1: Write the failing test**

```python
def test_logs_error_on_client_failure_and_reraises(caplog_tasks):
    reports = [ReportFactory.create() for _ in range(2)]
    pks = [r.pk for r in reports]
    fake = MagicMock()
    fake.__enter__ = MagicMock(return_value=fake)
    fake.__exit__ = MagicMock(return_value=None)
    fake.embed_documents = MagicMock(
        side_effect=EmbeddingClientError("service down")
    )

    with patch("radis.pgsearch.tasks.EmbeddingClient", return_value=fake):
        with pytest.raises(EmbeddingClientError):
            embed_reports_task(report_ids=pks)

    error_msgs = [r.getMessage() for r in caplog_tasks.records if r.levelname == "ERROR"]
    assert any(
        "embed_reports_task: embedding client failure after retries" in m
        for m in error_msgs
    )
```

- [ ] **Step 2: Run the failing test**

Run: `uv run cli test -- radis/pgsearch/tests/test_embed_reports_task.py::test_logs_error_on_client_failure_and_reraises -v`
Expected: FAIL — no matching ERROR record.

- [ ] **Step 3: Implement**

In `radis/pgsearch/tasks.py`, in `embed_reports_task`, replace:

```python
    with EmbeddingClient() as client:
        for start in range(0, len(rsvs), batch_size):
            chunk = rsvs[start : start + batch_size]
            _embed_with_bisect(client, chunk, embedded, skipped)
```

with:

```python
    try:
        with EmbeddingClient() as client:
            for start in range(0, len(rsvs), batch_size):
                chunk = rsvs[start : start + batch_size]
                _embed_with_bisect(client, chunk, embedded, skipped)
    except EmbeddingClientError as exc:
        logger.error(
            "embed_reports_task: embedding client failure after retries; "
            "report_ids=%s. Will be retried by Procrastinate. Error: %s",
            _truncate_ids(report_ids),
            exc,
        )
        raise
```

- [ ] **Step 4: Run the test**

Run: `uv run cli test -- radis/pgsearch/tests/test_embed_reports_task.py::test_logs_error_on_client_failure_and_reraises radis/pgsearch/tests/test_embed_reports_task.py::test_embedding_error_propagates radis/pgsearch/tests/test_embed_reports_task.py::test_non_too_large_error_propagates_without_bisecting -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add radis/pgsearch/tasks.py radis/pgsearch/tests/test_embed_reports_task.py
git commit -m "feat(pgsearch): log ERROR + re-raise on embedding client failure"
```

---

### Task 7: Truncate skipped-id list in existing ERROR

**Files:**
- Modify: `radis/pgsearch/tasks.py` (in `embed_reports_task`'s skipped-summary ERROR)

The existing ERROR currently passes the full list `[rsv.report.pk for rsv in skipped]`. Switch to `_truncate_ids(...)` so a worst-case 1000-report subjob doesn't dump a 1000-element list into a single log line. The existing test `test_bisects_on_too_large_and_isolates_offender` still passes because the assertion uses `str(offender_pk) in msg` and the truncated list still contains the single offender's id.

- [ ] **Step 1: Modify the existing ERROR**

In `radis/pgsearch/tasks.py`, find:

```python
    if skipped:
        logger.error(
            "embed_reports_task: %d report(s) skipped as too large for the embedding "
            "model; report_ids=%s. Fix the upstream report or raise the model context "
            "limit; their RSV rows stay NULL until embedded.",
            len(skipped),
            [rsv.report.pk for rsv in skipped],
        )
```

Replace the last argument:

```python
            _truncate_ids([rsv.report.pk for rsv in skipped]),
```

- [ ] **Step 2: Run existing bisect test to confirm assertion still holds**

Run: `uv run cli test -- radis/pgsearch/tests/test_embed_reports_task.py::test_bisects_on_too_large_and_isolates_offender -v`
Expected: PASS.

- [ ] **Step 3: Commit**

```bash
git add radis/pgsearch/tasks.py
git commit -m "refactor(pgsearch): truncate skipped-id list in embed_reports_task ERROR"
```

---

### Task 8: Add WARNING on multi-item bisect

**Files:**
- Modify: `radis/pgsearch/tasks.py` (in `_embed_with_bisect`)
- Modify: `radis/pgsearch/tests/test_embed_reports_task.py` (new test)

- [ ] **Step 1: Write the failing test**

```python
def test_logs_warning_on_multi_item_bisect(settings, caplog_tasks):
    settings.EMBEDDING_BATCH_SIZE = 4
    reports = [ReportFactory.create() for _ in range(4)]
    pks = [r.pk for r in reports]
    offender_pk = pks[2]
    vec = _unit_vec(settings.EMBEDDING_DIM)

    def fake_embed(texts):
        offender_body = (
            ReportSearchIndex.objects.select_related("report")
            .get(report_id=offender_pk).report.body
        )
        if offender_body in texts:
            raise EmbeddingPayloadTooLargeError("over context window")
        return [vec] * len(texts)

    fake = MagicMock()
    fake.__enter__ = MagicMock(return_value=fake)
    fake.__exit__ = MagicMock(return_value=None)
    fake.embed_documents = MagicMock(side_effect=fake_embed)

    with patch("radis.pgsearch.tasks.EmbeddingClient", return_value=fake):
        embed_reports_task(report_ids=pks)

    warning_msgs = [r.getMessage() for r in caplog_tasks.records if r.levelname == "WARNING"]
    assert any(
        "rejected as too large; bisecting" in m for m in warning_msgs
    )
```

- [ ] **Step 2: Run the failing test**

Run: `uv run cli test -- radis/pgsearch/tests/test_embed_reports_task.py::test_logs_warning_on_multi_item_bisect -v`
Expected: FAIL — no WARNING record.

- [ ] **Step 3: Implement**

In `radis/pgsearch/tasks.py`, in `_embed_with_bisect`, find:

```python
            skipped.append(offender)
            return
        mid = len(rsvs) // 2
        _embed_with_bisect(client, rsvs[:mid], embedded, skipped)
        _embed_with_bisect(client, rsvs[mid:], embedded, skipped)
        return
```

Insert the WARNING before the `mid = ...` line:

```python
            skipped.append(offender)
            return
        logger.warning(
            "embed_reports_task: chunk of %d report(s) rejected as too large; "
            "bisecting to isolate offender(s).",
            len(rsvs),
        )
        mid = len(rsvs) // 2
        _embed_with_bisect(client, rsvs[:mid], embedded, skipped)
        _embed_with_bisect(client, rsvs[mid:], embedded, skipped)
        return
```

- [ ] **Step 4: Run the test**

Run: `uv run cli test -- radis/pgsearch/tests/test_embed_reports_task.py::test_logs_warning_on_multi_item_bisect radis/pgsearch/tests/test_embed_reports_task.py::test_bisects_on_too_large_and_isolates_offender -v`
Expected: both PASS.

- [ ] **Step 5: Commit**

```bash
git add radis/pgsearch/tasks.py radis/pgsearch/tests/test_embed_reports_task.py
git commit -m "feat(pgsearch): log WARNING on payload-too-large bisect"
```

---

### Task 9: Add INFO log in `enqueue_embed_reports`

**Files:**
- Modify: `radis/pgsearch/tasks.py` (in `enqueue_embed_reports`, after the loop)
- Modify: `radis/pgsearch/tests/test_embed_reports_task.py` (new test)

- [ ] **Step 1: Write the failing test**

```python
def test_enqueue_embed_reports_logs_info_with_counts_and_priority(
    settings, caplog_tasks
):
    settings.EMBEDDING_SUBJOB_SIZE = 3
    with patch("radis.pgsearch.tasks.app.configure_task"):
        enqueue_embed_reports([1, 2, 3, 4, 5, 6, 7], priority=5)

    info_msgs = [r.getMessage() for r in caplog_tasks.records if r.levelname == "INFO"]
    assert any(
        "enqueue_embed_reports: deferred 3 subjob(s) for 7 report(s) at priority=5"
        in m
        for m in info_msgs
    )
```

- [ ] **Step 2: Run the failing test**

Run: `uv run cli test -- radis/pgsearch/tests/test_embed_reports_task.py::test_enqueue_embed_reports_logs_info_with_counts_and_priority -v`
Expected: FAIL.

- [ ] **Step 3: Implement**

In `radis/pgsearch/tasks.py`, in `enqueue_embed_reports`, before `return count`:

```python
    logger.info(
        "enqueue_embed_reports: deferred %d subjob(s) for %d report(s) at priority=%d",
        count,
        len(report_ids),
        priority,
    )
    return count
```

- [ ] **Step 4: Run the test**

Run: `uv run cli test -- radis/pgsearch/tests/test_embed_reports_task.py::test_enqueue_embed_reports_logs_info_with_counts_and_priority -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add radis/pgsearch/tasks.py radis/pgsearch/tests/test_embed_reports_task.py
git commit -m "feat(pgsearch): log INFO on enqueue_embed_reports deferral"
```

---

### Task 10: Define `_log_stamina_retry` hook function

**Files:**
- Modify: `radis/pgsearch/tasks.py` (add hook function near top, after `_truncate_ids`)
- Modify: `radis/pgsearch/tests/test_embed_reports_task.py` (unit test that the hook formats correctly)

This task defines the hook. The next task registers it.

- [ ] **Step 1: Write the failing test**

```python
def test_log_stamina_retry_emits_warning_for_embed_call(caplog_tasks):
    from stamina.instrumentation import RetryDetails

    from radis.pgsearch.tasks import _log_stamina_retry

    details = RetryDetails(
        name="radis.pgsearch.tasks._embed_chunk_with_retry",
        args=(),
        kwargs={},
        retry_num=2,
        wait_for=1.25,
        caused_by=RuntimeError("boom"),
    )
    _log_stamina_retry(details)

    warning_msgs = [r.getMessage() for r in caplog_tasks.records if r.levelname == "WARNING"]
    assert any(
        "embed_reports_task: embedding HTTP call failed; attempt=2 "
        "retrying in 1.25s. Error: boom" in m
        for m in warning_msgs
    )


def test_log_stamina_retry_ignores_other_callsites(caplog_tasks):
    from stamina.instrumentation import RetryDetails

    from radis.pgsearch.tasks import _log_stamina_retry

    details = RetryDetails(
        name="some.other.module._other_retry",
        args=(),
        kwargs={},
        retry_num=1,
        wait_for=0.5,
        caused_by=RuntimeError("not ours"),
    )
    _log_stamina_retry(details)

    warning_msgs = [r.getMessage() for r in caplog_tasks.records if r.levelname == "WARNING"]
    assert warning_msgs == []
```

- [ ] **Step 2: Run failing tests**

Run: `uv run cli test -- radis/pgsearch/tests/test_embed_reports_task.py::test_log_stamina_retry_emits_warning_for_embed_call radis/pgsearch/tests/test_embed_reports_task.py::test_log_stamina_retry_ignores_other_callsites -v`
Expected: both FAIL — `cannot import name '_log_stamina_retry'`.

- [ ] **Step 3: Implement**

In `radis/pgsearch/tasks.py`, change the imports at the top:

```python
import stamina
```

to:

```python
import stamina
import stamina.instrumentation
```

Then add this function below the existing `_embed_chunk_with_retry` decorator/definition (it references the fully-qualified callsite name; defining it after the target keeps that lookup obvious):

```python
def _log_stamina_retry(details: stamina.instrumentation.RetryDetails) -> None:
    """Stamina on_retry hook. Logs WARNING per retry attempt of the
    embedding HTTP call. Filters by callsite name so this hook stays a
    no-op for any other stamina-decorated function added later."""
    if details.name != "radis.pgsearch.tasks._embed_chunk_with_retry":
        return
    logger.warning(
        "embed_reports_task: embedding HTTP call failed; attempt=%d "
        "retrying in %.2fs. Error: %s",
        details.retry_num,
        details.wait_for,
        details.caused_by,
    )
```

- [ ] **Step 4: Run the tests**

Run: `uv run cli test -- radis/pgsearch/tests/test_embed_reports_task.py::test_log_stamina_retry_emits_warning_for_embed_call radis/pgsearch/tests/test_embed_reports_task.py::test_log_stamina_retry_ignores_other_callsites -v`
Expected: both PASS.

- [ ] **Step 5: Commit**

```bash
git add radis/pgsearch/tasks.py radis/pgsearch/tests/test_embed_reports_task.py
git commit -m "feat(pgsearch): add stamina on_retry hook for embedding call"
```

---

### Task 11: Register `_log_stamina_retry` in `PgSearchConfig.ready()`

**Files:**
- Modify: `radis/pgsearch/apps.py` (in `ready()`)
- Modify: `radis/pgsearch/tests/test_apps_checks.py` (new test)

- [ ] **Step 1: Write the failing test**

In `radis/pgsearch/tests/test_apps_checks.py`, at the bottom:

```python
def test_stamina_on_retry_hook_includes_log_stamina_retry():
    """`PgSearchConfig.ready()` registers our embed-call WARNING hook
    so stamina retries surface in logs."""
    from stamina.instrumentation import get_on_retry_hooks

    from radis.pgsearch.tasks import _log_stamina_retry

    assert _log_stamina_retry in get_on_retry_hooks()
```

- [ ] **Step 2: Run the failing test**

Run: `uv run cli test -- radis/pgsearch/tests/test_apps_checks.py::test_stamina_on_retry_hook_includes_log_stamina_retry -v`
Expected: FAIL — hook not in the registry.

- [ ] **Step 3: Implement**

In `radis/pgsearch/apps.py`, change:

```python
class PgSearchConfig(AppConfig):
    name = "radis.pgsearch"

    def ready(self):
        from . import signals as signals  # noqa: F401

        register_app()
```

to:

```python
class PgSearchConfig(AppConfig):
    name = "radis.pgsearch"

    def ready(self):
        import stamina.instrumentation

        from . import signals as signals  # noqa: F401
        from .tasks import _log_stamina_retry

        stamina.instrumentation.set_on_retry_hooks([_log_stamina_retry])

        register_app()
```

- [ ] **Step 4: Run the test**

Run: `uv run cli test -- radis/pgsearch/tests/test_apps_checks.py::test_stamina_on_retry_hook_includes_log_stamina_retry -v`
Expected: PASS.

- [ ] **Step 5: Verify the integration: stamina retry triggers the WARNING end-to-end**

Add this integration test in `radis/pgsearch/tests/test_embed_reports_task.py`:

```python
def test_stamina_retry_emits_warning_through_registered_hook(
    settings, stamina_active, caplog_tasks
):
    settings.EMBEDDING_BATCH_SIZE = 4
    reports = [ReportFactory.create() for _ in range(2)]
    pks = [r.pk for r in reports]
    vec = _unit_vec(settings.EMBEDDING_DIM)

    fake = MagicMock()
    fake.__enter__ = MagicMock(return_value=fake)
    fake.__exit__ = MagicMock(return_value=None)
    fake.embed_documents = MagicMock(
        side_effect=[EmbeddingClientError("blip"), [vec, vec]]
    )

    with patch("radis.pgsearch.tasks.EmbeddingClient", return_value=fake):
        embed_reports_task(report_ids=pks)

    warning_msgs = [r.getMessage() for r in caplog_tasks.records if r.levelname == "WARNING"]
    assert any(
        "embedding HTTP call failed; attempt=1" in m for m in warning_msgs
    )
```

- [ ] **Step 6: Run the integration test**

Run: `uv run cli test -- radis/pgsearch/tests/test_embed_reports_task.py::test_stamina_retry_emits_warning_through_registered_hook -v`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add radis/pgsearch/apps.py radis/pgsearch/tests/test_apps_checks.py radis/pgsearch/tests/test_embed_reports_task.py
git commit -m "feat(pgsearch): register stamina on_retry hook in app ready()"
```

---

### Task 12: Add INFO at ingest entry (`_index_reports`)

**Files:**
- Modify: `radis/pgsearch/apps.py` (in `_index_reports`)
- Modify: `radis/pgsearch/tests/test_apps_checks.py` (new test)

- [ ] **Step 1: Write the failing test**

In `radis/pgsearch/tests/test_apps_checks.py`, add at the bottom:

```python
import logging
from unittest.mock import MagicMock, patch


def test_index_reports_logs_info_with_mode_and_count(settings, caplog):
    from radis.pgsearch.apps import _index_reports

    apps_logger = logging.getLogger("radis.pgsearch.apps")
    apps_logger.addHandler(caplog.handler)
    caplog.set_level(logging.INFO, logger="radis.pgsearch.apps")
    try:
        settings.PGSEARCH_SYNC_INDEXING = False
        reports = [MagicMock(pk=1), MagicMock(pk=2), MagicMock(pk=3)]
        with patch("radis.pgsearch.tasks.enqueue_bulk_index_reports"):
            _index_reports(reports)
    finally:
        apps_logger.removeHandler(caplog.handler)

    info_msgs = [r.getMessage() for r in caplog.records if r.levelname == "INFO"]
    assert any(
        "pgsearch.index_reports: handler invoked; reports=3 mode=async" in m
        for m in info_msgs
    )
```

- [ ] **Step 2: Run the failing test**

Run: `uv run cli test -- radis/pgsearch/tests/test_apps_checks.py::test_index_reports_logs_info_with_mode_and_count -v`
Expected: FAIL — apps.py has no logger yet.

- [ ] **Step 3: Implement**

In `radis/pgsearch/apps.py`, add at the top with the other imports:

```python
import logging

from django.apps import AppConfig
from django.conf import settings
from django.core.checks import Error, register

logger = logging.getLogger(__name__)
```

Then in `_index_reports`, after the empty-input early return:

```python
def _index_reports(reports):
    """..."""
    if not reports:
        return

    logger.info(
        "pgsearch.index_reports: handler invoked; reports=%d mode=%s",
        len(reports),
        "sync" if settings.PGSEARCH_SYNC_INDEXING else "async",
    )

    from radis.pgsearch.tasks import enqueue_bulk_index_reports, enqueue_embed_reports
    from radis.pgsearch.utils.indexing import bulk_upsert_report_search_indexes

    # ... rest unchanged ...
```

- [ ] **Step 4: Run the test**

Run: `uv run cli test -- radis/pgsearch/tests/test_apps_checks.py::test_index_reports_logs_info_with_mode_and_count -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add radis/pgsearch/apps.py radis/pgsearch/tests/test_apps_checks.py
git commit -m "feat(pgsearch): log INFO at ingest handler entry"
```

---

### Task 13: Add INFO logs in `embed_pending` management command

**Files:**
- Modify: `radis/pgsearch/management/commands/embed_pending.py`
- Modify: `radis/pgsearch/tests/test_embed_pending_command.py`

- [ ] **Step 1: Write the failing test**

In `radis/pgsearch/tests/test_embed_pending_command.py`, add at the bottom:

```python
import logging


def test_logs_info_at_invoke_and_done(caplog):
    cmd_logger = logging.getLogger(
        "radis.pgsearch.management.commands.embed_pending"
    )
    cmd_logger.addHandler(caplog.handler)
    caplog.set_level(logging.INFO, logger=cmd_logger.name)
    try:
        [ReportFactory.create() for _ in range(2)]
        out = StringIO()
        with patch(
            "radis.pgsearch.management.commands.embed_pending.enqueue_embed_reports",
            return_value=1,
        ):
            call_command("embed_pending", "--subjob-size", "5", stdout=out)
    finally:
        cmd_logger.removeHandler(caplog.handler)

    info_msgs = [r.getMessage() for r in caplog.records if r.levelname == "INFO"]
    assert any(
        "embed_pending: command invoked; subjob_size=5 limit=None" in m
        for m in info_msgs
    )
    assert any(
        "embed_pending: done; reports=2 subjobs=1" in m for m in info_msgs
    )
```

- [ ] **Step 2: Run the failing test**

Run: `uv run cli test -- radis/pgsearch/tests/test_embed_pending_command.py::test_logs_info_at_invoke_and_done -v`
Expected: FAIL.

- [ ] **Step 3: Implement**

In `radis/pgsearch/management/commands/embed_pending.py`, add near the top imports:

```python
import logging

from django.conf import settings
from django.core.management.base import BaseCommand

from radis.pgsearch.models import ReportSearchIndex
from radis.pgsearch.tasks import enqueue_embed_reports

logger = logging.getLogger(__name__)
```

In `handle()`, restructure so logging happens around the existing logic:

```python
    def handle(self, *args, **opts) -> None:
        subjob_size = opts["subjob_size"]
        logger.info(
            "embed_pending: command invoked; subjob_size=%d limit=%s",
            subjob_size,
            opts["limit"],
        )

        ids = list(
            ReportSearchIndex.objects.filter(embedding__isnull=True)
            .order_by("report_id")
            .values_list("report_id", flat=True)
        )
        if opts["limit"] is not None:
            ids = ids[: opts["limit"]]
        if not ids:
            self.stdout.write("Nothing to embed.")
            return

        self.stdout.write(
            f"Enqueuing {len(ids)} report(s) in subjobs of {subjob_size}..."
        )
        subjob_count = enqueue_embed_reports(
            ids,
            subjob_size=subjob_size,
            priority=settings.EMBEDDING_BACKFILL_PRIORITY,
        )
        self.stdout.write(
            self.style.SUCCESS(f"Done. Deferred {subjob_count} subjob(s).")
        )
        logger.info(
            "embed_pending: done; reports=%d subjobs=%d",
            len(ids),
            subjob_count,
        )
```

- [ ] **Step 4: Run the test**

Run: `uv run cli test -- radis/pgsearch/tests/test_embed_pending_command.py -v`
Expected: all tests PASS (including the existing ones).

- [ ] **Step 5: Commit**

```bash
git add radis/pgsearch/management/commands/embed_pending.py radis/pgsearch/tests/test_embed_pending_command.py
git commit -m "feat(pgsearch): log INFO at embed_pending invoke and done"
```

---

### Task 14: Add INFO in admin `enqueue_pending_embeddings` action

**Files:**
- Modify: `radis/pgsearch/admin.py`
- Modify: `radis/pgsearch/tests/test_admin.py`

- [ ] **Step 1: Write the failing test**

In `radis/pgsearch/tests/test_admin.py`, add at the bottom:

```python
import logging
from unittest.mock import patch


def test_enqueue_pending_embeddings_logs_info_with_user_and_counts(caplog):
    admin_logger = logging.getLogger("radis.pgsearch.admin")
    admin_logger.addHandler(caplog.handler)
    caplog.set_level(logging.INFO, logger="radis.pgsearch.admin")
    try:
        targets = [ReportFactory.create() for _ in range(2)]
        selected = ReportSearchIndex.objects.filter(
            report_id__in=[r.pk for r in targets]
        )
        request = MagicMock()
        request.user.username = "alice"

        admin_instance = ReportSearchIndexAdmin(ReportSearchIndex, AdminSite())
        admin_instance.message_user = MagicMock()
        with patch(
            "radis.pgsearch.admin.enqueue_embed_reports", return_value=1
        ):
            admin_instance.enqueue_pending_embeddings(request, selected)
    finally:
        admin_logger.removeHandler(caplog.handler)

    info_msgs = [r.getMessage() for r in caplog.records if r.levelname == "INFO"]
    assert any(
        "admin.enqueue_pending_embeddings: user=alice enqueued 2 report(s) "
        "across 1 subjob(s)" in m
        for m in info_msgs
    )
```

- [ ] **Step 2: Run the failing test**

Run: `uv run cli test -- radis/pgsearch/tests/test_admin.py::test_enqueue_pending_embeddings_logs_info_with_user_and_counts -v`
Expected: FAIL — `admin.py` has no logger yet.

- [ ] **Step 3: Implement**

In `radis/pgsearch/admin.py`, add at the top:

```python
import logging

from django.conf import settings
from django.contrib import admin, messages
# ... rest of imports unchanged ...

logger = logging.getLogger(__name__)
```

In `enqueue_pending_embeddings`, after the existing `self.message_user(...)` call, add:

```python
        logger.info(
            "admin.enqueue_pending_embeddings: user=%s enqueued %d report(s) "
            "across %d subjob(s)",
            request.user.username,
            len(report_ids),
            subjob_count,
        )
```

- [ ] **Step 4: Run the test**

Run: `uv run cli test -- radis/pgsearch/tests/test_admin.py::test_enqueue_pending_embeddings_logs_info_with_user_and_counts -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add radis/pgsearch/admin.py radis/pgsearch/tests/test_admin.py
git commit -m "feat(pgsearch): log INFO on admin enqueue_pending_embeddings action"
```

---

### Task 15: Add INFO in admin `clear_embeddings_for_remodel` action

**Files:**
- Modify: `radis/pgsearch/admin.py`
- Modify: `radis/pgsearch/tests/test_admin.py`

- [ ] **Step 1: Write the failing test**

In `radis/pgsearch/tests/test_admin.py`, add at the bottom:

```python
def test_clear_embeddings_for_remodel_logs_info_with_user_and_count(caplog):
    admin_logger = logging.getLogger("radis.pgsearch.admin")
    admin_logger.addHandler(caplog.handler)
    caplog.set_level(logging.INFO, logger="radis.pgsearch.admin")
    try:
        targets = [ReportFactory.create() for _ in range(2)]
        for r in targets:
            rsi = ReportSearchIndex.objects.get(report_id=r.pk)
            rsi.embedding = [0.1] * 1024
            rsi.save()
        selected = ReportSearchIndex.objects.filter(
            report_id__in=[r.pk for r in targets]
        )
        request = MagicMock()
        request.user.username = "bob"

        admin_instance = ReportSearchIndexAdmin(ReportSearchIndex, AdminSite())
        admin_instance.message_user = MagicMock()
        admin_instance.clear_embeddings_for_remodel(request, selected)
    finally:
        admin_logger.removeHandler(caplog.handler)

    info_msgs = [r.getMessage() for r in caplog.records if r.levelname == "INFO"]
    assert any(
        "admin.clear_embeddings_for_remodel: user=bob cleared 2 embedding(s)" in m
        for m in info_msgs
    )
```

- [ ] **Step 2: Run the failing test**

Run: `uv run cli test -- radis/pgsearch/tests/test_admin.py::test_clear_embeddings_for_remodel_logs_info_with_user_and_count -v`
Expected: FAIL.

- [ ] **Step 3: Implement**

In `radis/pgsearch/admin.py`, in `clear_embeddings_for_remodel`, after the existing `self.message_user(...)` call:

```python
        logger.info(
            "admin.clear_embeddings_for_remodel: user=%s cleared %d embedding(s)",
            request.user.username,
            cleared,
        )
```

- [ ] **Step 4: Run the test**

Run: `uv run cli test -- radis/pgsearch/tests/test_admin.py::test_clear_embeddings_for_remodel_logs_info_with_user_and_count -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add radis/pgsearch/admin.py radis/pgsearch/tests/test_admin.py
git commit -m "feat(pgsearch): log INFO on admin clear_embeddings_for_remodel action"
```

---

### Task 16: Final verification

- [ ] **Step 1: Run lint**

Run: `uv run cli lint`
Expected: "0 errors, 0 warnings, 0 informations".

- [ ] **Step 2: Run the full pgsearch test suite**

Run: `uv run cli test -- radis/pgsearch/tests/ -v`
Expected: all PASS.

- [ ] **Step 3: Manual smoke (only if dev stack is running)**

Run: `uv run cli compose-up -- --watch` (skip if already running)
Then ingest a small batch:

```bash
uv run cli generate-example-reports --count 3
```

Tail the worker logs:

```bash
docker compose logs -f embeddings_worker default_worker | grep -E "pgsearch|embed_reports_task|enqueue_embed_reports"
```

Expected sequence (timestamps elided):
```
[radis.pgsearch.apps] INFO pgsearch.index_reports: handler invoked; reports=3 mode=async
[radis.pgsearch.tasks] INFO Indexing 3 reports in bulk.
[radis.pgsearch.tasks] INFO enqueue_embed_reports: deferred 1 subjob(s) for 3 report(s) at priority=1
[radis.pgsearch.tasks] INFO embed_reports_task: start; reports=3
[radis.pgsearch.tasks] INFO embed_reports_task: finished; embedded=3 skipped=0 duration_ms=...
```

- [ ] **Step 4: Manual smoke — service-down case (optional)**

With dev stack running, stop the embedding service (`docker compose stop llamacpp` or set `EMBEDDING_PROVIDER_URL` to an unreachable host and restart `embeddings_worker`), enqueue some embed work, then watch logs.

Expected: WARNING per stamina retry (`attempt=1 retrying in 0.50s`), then ERROR `embedding client failure after retries`.

- [ ] **Step 5: No commit unless smoke uncovered something.**

---

## Self-review

**Spec coverage check (against the spec's "Coverage map"):**

- Row 1 `_index_reports` INFO → Task 12 ✅
- Row 5 `enqueue_embed_reports` INFO → Task 9 ✅
- Row 6 `embed_reports_task` start/finish/duration/client-failure → Tasks 4, 5, 6 ✅
- Row 7 `_embed_with_bisect` WARNING → Task 8 ✅
- Row 8 `_embed_chunk_with_retry` stamina hook → Tasks 10 + 11 ✅
- Row 10 `embed_pending` INFOs → Task 13 ✅
- Row 11 admin `enqueue_pending_embeddings` INFO → Task 14 ✅
- Row 12 admin `clear_embeddings_for_remodel` INFO → Task 15 ✅
- `_truncate_ids` helper → Tasks 3 + 7 ✅

**Placeholder scan:** No TBD/TODO/"similar to" placeholders. Every code change shows the exact code.

**Type/name consistency:** Hook function is `_log_stamina_retry` everywhere. Helper is `_truncate_ids(ids, limit=50)` everywhere. Log prefix conventions (`embed_reports_task: ...`, `enqueue_embed_reports: ...`, `pgsearch.index_reports: ...`, `embed_pending: ...`, `admin.<action>: ...`) match the spec verbatim.
