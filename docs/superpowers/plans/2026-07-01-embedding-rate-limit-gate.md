# Embedding Rate-Limit Gate Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the Postgres-backed sliding-window rate-limit gate for embedding requests, referenced but never implemented in `tasks.py`'s retry predicate comment, so the embeddings background task and live search share a coordinated 60/min budget with search getting priority.

**Architecture:** A new append-only ledger table (`EmbeddingRateLimitEvent`) records each admitted request's send time and weight; `radis/pgsearch/utils/rate_limiter.py` provides the acquisition/spillover/retry logic on top of it; two existing call sites (`_embed_chunk_with_retry` in `tasks.py` for background bulk embedding, `embed_query()` in `embedding_client.py` for search/retrieval) get wrapped to go through the gate.

**Tech Stack:** Django ORM + a raw `pg_advisory_xact_lock` call for cross-process serialization, `openai` SDK exception types, pytest + pytest-django.

## Global Constraints

- Python style: Google Python Style Guide; Ruff line length 100 characters (per `CLAUDE.md`).
- Type checking: pyright in basic mode (per `CLAUDE.md`) — all new code must have complete type annotations on function signatures.
- Retry/attempt budgets in this codebase use `max_attempts=3` as the standard convention (matches `stamina.retry(attempts=3, ...)` elsewhere in `tasks.py`) — the gate's reactive retry uses the same number.
- Full spec: `docs/superpowers/specs/2026-07-01-embedding-rate-limit-gate-design.md`. Read it before starting if anything below is unclear on *why*, not just *what*.
- Test commands in this repo: `uv run pytest <path> -v` for a specific file, `uv run cli test -- -k <name>` for a specific test by name (per `CLAUDE.md`).

---

### Task 1: `EmbeddingRateLimitEvent` model and migration

**Files:**
- Modify: `radis/pgsearch/models.py`
- Create: `radis/pgsearch/migrations/0004_embeddingratelimitevent.py`

**Interfaces:**
- Produces: `radis.pgsearch.models.EmbeddingRateLimitEvent` with fields `bucket: str`, `sent_at: datetime`, `weight: int` — consumed by Task 2's `rate_limiter.py`.

- [ ] **Step 1: Add the model**

Append to the end of `radis/pgsearch/models.py` (after the existing `ReportSearchIndex.save` method, which currently ends at line 52):

```python


class EmbeddingRateLimitEvent(models.Model):
    """Sliding-window ledger for the embedding gateway's rate limit.

    Confirmed empirically against the production gateway: a genuine sliding
    window of ~60 request-equivalents/minute, where each admitted request
    independently expires exactly 60 seconds after *it* was recorded — not a
    fixed window, not a continuous per-second refill, not tied to request
    completion. See the design doc for how this was confirmed
    (docs/superpowers/specs/2026-07-01-embedding-rate-limit-gate-design.md).

    Rows older than the 60s window are pruned opportunistically by every
    acquisition attempt in `radis.pgsearch.utils.rate_limiter`, so this table
    stays small automatically — no separate cleanup job needed.
    """

    bucket = models.CharField(max_length=32)
    sent_at = models.DateTimeField()
    weight = models.PositiveIntegerField(default=1)

    class Meta:
        indexes = [models.Index(fields=["bucket", "sent_at"])]
```

- [ ] **Step 2: Write the migration**

Create `radis/pgsearch/migrations/0004_embeddingratelimitevent.py`:

```python
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("pgsearch", "0003_pending_embedding_index"),
    ]

    operations = [
        migrations.CreateModel(
            name="EmbeddingRateLimitEvent",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("bucket", models.CharField(max_length=32)),
                ("sent_at", models.DateTimeField()),
                ("weight", models.PositiveIntegerField(default=1)),
            ],
            options={
                "indexes": [
                    models.Index(
                        fields=["bucket", "sent_at"],
                        name="pgsearch_ratelimit_bucket_idx",
                    )
                ],
            },
        ),
    ]
```

- [ ] **Step 3: Verify the migration is consistent with the model**

Run: `uv run python manage.py makemigrations pgsearch --check --dry-run`
Expected: `No changes detected` (confirms the hand-written migration matches the model exactly — if it reports changes, fix the migration to match what Django would generate).

- [ ] **Step 4: Apply the migration to the test database and confirm it runs cleanly**

Run: `uv run python manage.py migrate pgsearch`
Expected: `Applying pgsearch.0004_embeddingratelimitevent... OK`

- [ ] **Step 5: Commit**

```bash
git add radis/pgsearch/models.py radis/pgsearch/migrations/0004_embeddingratelimitevent.py
git commit -m "feat(pgsearch): add EmbeddingRateLimitEvent model for the rate-limit gate"
```

---

### Task 2: Settings + core sliding-window acquisition (`acquire_token`, `try_acquire_immediate`)

**Files:**
- Modify: `radis/settings/base.py:357` (after `EMBEDDING_BACKFILL_PRIORITY`)
- Create: `radis/pgsearch/utils/rate_limiter.py`
- Create: `radis/pgsearch/tests/test_rate_limiter.py`

**Interfaces:**
- Consumes: `radis.pgsearch.models.EmbeddingRateLimitEvent` (Task 1).
- Produces: `configured_capacity(bucket: str) -> int`, `acquire_token(bucket: str, weight: int = 1) -> None`, `try_acquire_immediate(bucket: str, weight: int = 1) -> bool` — consumed by Task 3 (spillover) and Tasks 6/7 (call sites). Also produces internal seams `_now()` and `_sleep(seconds)` that tests monkeypatch to avoid real waiting.

- [ ] **Step 1: Add the two new settings**

In `radis/settings/base.py`, after line 357 (`EMBEDDING_BACKFILL_PRIORITY = 0`) and before the blank line preceding `# Hybrid search tuning`:

```python
EMBEDDING_LIVE_PRIORITY = 1
EMBEDDING_BACKFILL_PRIORITY = 0
# Rate-limit gate split of the embedding gateway's ~60 request/min budget
# (confirmed empirically; see docs/superpowers/specs/2026-07-01-embedding-rate-limit-gate-design.md).
# Search/retrieval is low-volume and interactive (a blocked live search is a
# real UX cost); background bulk embedding is high-volume and non-interactive
# (a delay is invisible). Search gets a reserved floor plus spillover into
# background's spare capacity; background is capped to its own share and can
# never borrow from search's floor. Tunable — leave headroom below the true
# ceiling given unconfirmed cost-weighting on large requests.
EMBEDDING_SEARCH_RATE_LIMIT_PER_MINUTE = 10
EMBEDDING_BACKGROUND_RATE_LIMIT_PER_MINUTE = 50
```

- [ ] **Step 2: Write the failing tests for capacity enforcement and independent expiry**

Create `radis/pgsearch/tests/test_rate_limiter.py`:

```python
"""Tests for radis.pgsearch.utils.rate_limiter."""

from datetime import timedelta

import pytest
from django.utils import timezone

pytestmark = pytest.mark.django_db


class _FakeClock:
    """Deterministic, controllable clock for testing sliding-window timing
    without real sleeps. `sleep()` advances the fake clock instead of
    blocking, matching what the code under test actually needs from time."""

    def __init__(self, start):
        self.current = start

    def now(self):
        return self.current

    def sleep(self, seconds):
        self.current += timedelta(seconds=seconds)


def _install_fake_clock(monkeypatch):
    from radis.pgsearch.utils import rate_limiter as rl

    clock = _FakeClock(timezone.now())
    monkeypatch.setattr(rl, "_now", clock.now)
    monkeypatch.setattr(rl, "_sleep", clock.sleep)
    return clock


def test_acquire_token_admits_within_capacity(settings, monkeypatch):
    settings.EMBEDDING_BACKGROUND_RATE_LIMIT_PER_MINUTE = 3
    from radis.pgsearch.utils import rate_limiter as rl

    _install_fake_clock(monkeypatch)

    rl.acquire_token("embedding_background")
    rl.acquire_token("embedding_background")
    rl.acquire_token("embedding_background")

    from radis.pgsearch.models import EmbeddingRateLimitEvent

    assert EmbeddingRateLimitEvent.objects.filter(bucket="embedding_background").count() == 3


def test_acquire_token_waits_for_capacity_then_succeeds(settings, monkeypatch):
    settings.EMBEDDING_BACKGROUND_RATE_LIMIT_PER_MINUTE = 1
    from radis.pgsearch.utils import rate_limiter as rl

    clock = _install_fake_clock(monkeypatch)

    rl.acquire_token("embedding_background")
    start = clock.current
    rl.acquire_token("embedding_background")

    assert (clock.current - start).total_seconds() >= 59


def test_try_acquire_immediate_returns_false_without_waiting(settings, monkeypatch):
    settings.EMBEDDING_BACKGROUND_RATE_LIMIT_PER_MINUTE = 1
    from radis.pgsearch.utils import rate_limiter as rl

    clock = _install_fake_clock(monkeypatch)
    sleep_calls = []
    monkeypatch.setattr(rl, "_sleep", lambda seconds: sleep_calls.append(seconds))

    rl.acquire_token("embedding_background")
    before = clock.current
    result = rl.try_acquire_immediate("embedding_background")

    assert result is False
    assert sleep_calls == []
    assert clock.current == before  # non-blocking: clock must not advance


def test_two_waves_recover_independently(settings, monkeypatch):
    """Mirrors the empirical two-wave test: tokens taken at different times
    must expire independently, not all at once or on a shared timer."""
    settings.EMBEDDING_BACKGROUND_RATE_LIMIT_PER_MINUTE = 5
    from radis.pgsearch.utils import rate_limiter as rl

    clock = _install_fake_clock(monkeypatch)

    # Wave A: 3 tokens at t=0.
    for _ in range(3):
        rl.acquire_token("embedding_background")
    # Wave B: 2 more at t=30 (fills capacity to 5).
    clock.current += timedelta(seconds=30)
    for _ in range(2):
        rl.acquire_token("embedding_background")

    # A 6th request at t=30 must wait — capacity is full.
    sleep_calls = []
    real_sleep = rl._sleep
    monkeypatch.setattr(
        rl, "_sleep", lambda seconds: (sleep_calls.append(seconds), real_sleep(seconds))
    )
    rl.acquire_token("embedding_background")
    # Wave A's oldest (t=0) expires at t=60; we were at t=30, so the wait
    # should be close to 30s, not close to 0 or close to 60.
    assert sleep_calls and 25 <= sleep_calls[0] <= 31

    from radis.pgsearch.models import EmbeddingRateLimitEvent

    assert EmbeddingRateLimitEvent.objects.filter(bucket="embedding_background").count() == 5
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `uv run pytest radis/pgsearch/tests/test_rate_limiter.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'radis.pgsearch.utils.rate_limiter'`

- [ ] **Step 4: Implement the core module**

Create `radis/pgsearch/utils/rate_limiter.py`:

```python
"""Postgres-backed sliding-window rate limiter for the embedding gateway.

See docs/superpowers/specs/2026-07-01-embedding-rate-limit-gate-design.md for
the empirical findings that drove this design (confirmed: a true sliding
window keyed to request send-time, not a fixed window or continuous refill;
cost possibly weighted by request size, exact formula unconfirmed).
"""

from __future__ import annotations

import time
from datetime import timedelta

from django.conf import settings
from django.db import connection, transaction
from django.db.models import Sum
from django.utils import timezone

from ..models import EmbeddingRateLimitEvent

_WINDOW = timedelta(seconds=60)

_CAPACITY_SETTINGS = {
    "embedding_search": "EMBEDDING_SEARCH_RATE_LIMIT_PER_MINUTE",
    "embedding_background": "EMBEDDING_BACKGROUND_RATE_LIMIT_PER_MINUTE",
}


def configured_capacity(bucket: str) -> int:
    return getattr(settings, _CAPACITY_SETTINGS[bucket])


def _now():
    """Seam so tests can inject a controllable clock instead of real time."""
    return timezone.now()


def _sleep(seconds: float) -> None:
    """Seam so tests can intercept waits instead of really blocking."""
    time.sleep(seconds)


def _advisory_lock(bucket: str) -> None:
    """Postgres transaction-scoped advisory lock keyed on the bucket name.

    Serializes concurrent acquisition attempts across every process sharing
    this database (web, llm_worker, embeddings_worker) — released
    automatically when the enclosing transaction ends."""
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", [bucket])


def _try_acquire_once(bucket: str, weight: int) -> tuple[bool, float]:
    """Single attempt: lock, prune expired rows, sum current weight, and
    either admit (insert a row) or report how long until enough capacity
    frees up. Returns (acquired, wait_seconds); wait_seconds is 0.0 when
    acquired."""
    with transaction.atomic():
        _advisory_lock(bucket)
        now = _now()
        window_start = now - _WINDOW
        EmbeddingRateLimitEvent.objects.filter(
            bucket=bucket, sent_at__lt=window_start
        ).delete()
        current_weight = (
            EmbeddingRateLimitEvent.objects.filter(bucket=bucket).aggregate(total=Sum("weight"))[
                "total"
            ]
            or 0
        )
        if current_weight + weight <= configured_capacity(bucket):
            EmbeddingRateLimitEvent.objects.create(bucket=bucket, sent_at=now, weight=weight)
            return True, 0.0

        oldest = EmbeddingRateLimitEvent.objects.filter(bucket=bucket).order_by("sent_at").first()
        if oldest is None:
            # Only reachable if weight alone exceeds capacity with an empty
            # bucket — nothing to wait on, so retry almost immediately.
            return False, 0.1
        wait_for = (oldest.sent_at + _WINDOW - now).total_seconds()
        return False, max(wait_for, 0.1)


def try_acquire_immediate(bucket: str, weight: int = 1) -> bool:
    """Non-blocking: returns whether capacity was available right now,
    without waiting if not."""
    acquired, _ = _try_acquire_once(bucket, weight)
    return acquired


def acquire_token(bucket: str, weight: int = 1) -> None:
    """Blocking: waits until capacity is available, then admits."""
    while True:
        acquired, wait_for = _try_acquire_once(bucket, weight)
        if acquired:
            return
        _sleep(wait_for)
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest radis/pgsearch/tests/test_rate_limiter.py -v`
Expected: 4 passed

- [ ] **Step 6: Commit**

```bash
git add radis/settings/base.py radis/pgsearch/utils/rate_limiter.py radis/pgsearch/tests/test_rate_limiter.py
git commit -m "feat(pgsearch): add core sliding-window rate limiter for embeddings"
```

---

### Task 3: Search-priority spillover (`acquire_search_priority_token`)

**Files:**
- Modify: `radis/pgsearch/utils/rate_limiter.py` (append)
- Modify: `radis/pgsearch/tests/test_rate_limiter.py` (append)

**Interfaces:**
- Consumes: `try_acquire_immediate`, `acquire_token` (Task 2).
- Produces: `acquire_search_priority_token(weight: int = 1) -> None` — consumed by Task 7 (`embed_query` wiring).

- [ ] **Step 1: Write the failing tests**

Append to `radis/pgsearch/tests/test_rate_limiter.py`:

```python
def test_search_priority_spillover_uses_background_when_search_exhausted(settings, monkeypatch):
    settings.EMBEDDING_SEARCH_RATE_LIMIT_PER_MINUTE = 1
    settings.EMBEDDING_BACKGROUND_RATE_LIMIT_PER_MINUTE = 2
    from radis.pgsearch.utils import rate_limiter as rl

    _install_fake_clock(monkeypatch)

    rl.acquire_token("embedding_search")  # exhausts the search floor
    rl.acquire_search_priority_token()  # must spill into background

    from radis.pgsearch.models import EmbeddingRateLimitEvent

    assert EmbeddingRateLimitEvent.objects.filter(bucket="embedding_search").count() == 1
    assert EmbeddingRateLimitEvent.objects.filter(bucket="embedding_background").count() == 1


def test_search_priority_waits_on_search_when_both_exhausted(settings, monkeypatch):
    settings.EMBEDDING_SEARCH_RATE_LIMIT_PER_MINUTE = 1
    settings.EMBEDDING_BACKGROUND_RATE_LIMIT_PER_MINUTE = 1
    from radis.pgsearch.utils import rate_limiter as rl

    clock = _install_fake_clock(monkeypatch)

    rl.acquire_token("embedding_search")
    rl.acquire_token("embedding_background")
    start = clock.current
    rl.acquire_search_priority_token()

    # Waited on search's own bucket (~60s), not left permanently blocked.
    assert (clock.current - start).total_seconds() >= 59

    from radis.pgsearch.models import EmbeddingRateLimitEvent

    assert EmbeddingRateLimitEvent.objects.filter(bucket="embedding_search").count() == 2
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest radis/pgsearch/tests/test_rate_limiter.py -v -k priority`
Expected: FAIL with `AttributeError: module 'radis.pgsearch.utils.rate_limiter' has no attribute 'acquire_search_priority_token'`

- [ ] **Step 3: Implement the spillover function**

Append to `radis/pgsearch/utils/rate_limiter.py`:

```python


def acquire_search_priority_token(weight: int = 1) -> None:
    """Search/retrieval acquisition: try the reserved search floor first,
    then opportunistically spill into background's spare capacity, and only
    block (waiting on the search bucket specifically) if both are exhausted.
    Background's own acquisition path never references "embedding_search",
    so it structurally cannot borrow from this reserved floor."""
    if try_acquire_immediate("embedding_search", weight):
        return
    if try_acquire_immediate("embedding_background", weight):
        return
    acquire_token("embedding_search", weight)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest radis/pgsearch/tests/test_rate_limiter.py -v`
Expected: 6 passed

- [ ] **Step 5: Commit**

```bash
git add radis/pgsearch/utils/rate_limiter.py radis/pgsearch/tests/test_rate_limiter.py
git commit -m "feat(pgsearch): add search-priority spillover to the rate limiter"
```

---

### Task 4: Parse the gateway's authoritative `Wait Xs` / `Retry-After`

**Files:**
- Modify: `radis/pgsearch/utils/rate_limiter.py` (append)
- Modify: `radis/pgsearch/tests/test_rate_limiter.py` (append)

**Interfaces:**
- Consumes: `openai.RateLimitError` (SDK type).
- Produces: `parse_retry_after(exc: openai.RateLimitError) -> float` — consumed by Task 5 (`call_with_rate_limit`).

- [ ] **Step 1: Write the failing tests**

Append to `radis/pgsearch/tests/test_rate_limiter.py`:

```python
def _make_rate_limit_error(message: str, retry_after: str | None = None) -> "openai.RateLimitError":
    import httpx
    import openai

    request = httpx.Request("POST", "http://embed.example/v1/embeddings")
    headers = {"Retry-After": retry_after} if retry_after else {}
    response = httpx.Response(429, headers=headers, request=request, json={"detail": message})
    return openai.RateLimitError(message=message, response=response, body=None)


def test_parse_retry_after_uses_header_when_present():
    from radis.pgsearch.utils import rate_limiter as rl

    exc = _make_rate_limit_error("Limit 60/min exceeded. Wait 27s.", retry_after="12")

    assert rl.parse_retry_after(exc) == 12.0


def test_parse_retry_after_falls_back_to_wait_message():
    from radis.pgsearch.utils import rate_limiter as rl

    exc = _make_rate_limit_error("Limit 60/min exceeded. Wait 27s.")

    assert rl.parse_retry_after(exc) == 27.0


def test_parse_retry_after_default_when_neither_present():
    from radis.pgsearch.utils import rate_limiter as rl

    exc = _make_rate_limit_error("rate limited")

    assert rl.parse_retry_after(exc) == rl._DEFAULT_RETRY_AFTER
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest radis/pgsearch/tests/test_rate_limiter.py -v -k parse_retry_after`
Expected: FAIL with `AttributeError: module 'radis.pgsearch.utils.rate_limiter' has no attribute 'parse_retry_after'`

- [ ] **Step 3: Implement `parse_retry_after`**

At the top of `radis/pgsearch/utils/rate_limiter.py`, change the import block:

```python
from __future__ import annotations

import time
from datetime import timedelta

from django.conf import settings
from django.db import connection, transaction
from django.db.models import Sum
from django.utils import timezone

from ..models import EmbeddingRateLimitEvent
```

to:

```python
from __future__ import annotations

import re
import time
from datetime import timedelta

import openai
from django.conf import settings
from django.db import connection, transaction
from django.db.models import Sum
from django.utils import timezone

from ..models import EmbeddingRateLimitEvent
```

Then append to the end of the file:

```python


_WAIT_RE = re.compile(r"[Ww]ait (\d+(?:\.\d+)?)s")
_DEFAULT_RETRY_AFTER = 5.0


def parse_retry_after(exc: openai.RateLimitError) -> float:
    """Extract the gateway's own authoritative wait time from a 429: the
    standard HTTP `Retry-After` header first, then the `"Wait Xs"` phrasing
    this specific gateway uses in its response body, then a conservative
    default if neither is present."""
    response = getattr(exc, "response", None)
    if response is not None:
        header = response.headers.get("Retry-After")
        if header is not None:
            try:
                return float(header)
            except ValueError:
                pass
    match = _WAIT_RE.search(str(exc))
    if match:
        return float(match.group(1))
    return _DEFAULT_RETRY_AFTER
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest radis/pgsearch/tests/test_rate_limiter.py -v`
Expected: 9 passed

- [ ] **Step 5: Commit**

```bash
git add radis/pgsearch/utils/rate_limiter.py radis/pgsearch/tests/test_rate_limiter.py
git commit -m "feat(pgsearch): parse the embedding gateway's authoritative retry-after"
```

---

### Task 5: Reactive retry wrapper (`call_with_rate_limit`)

**Files:**
- Modify: `radis/pgsearch/utils/rate_limiter.py` (append)
- Modify: `radis/pgsearch/tests/test_rate_limiter.py` (append)

**Interfaces:**
- Consumes: `parse_retry_after` (Task 4), `_sleep` (Task 2, internal seam).
- Produces: `call_with_rate_limit(acquire_fn: Callable[[], None], fn: Callable[[], T], max_attempts: int = 3) -> T` — consumed by Task 6 (`_embed_chunk_with_retry`) and Task 7 (`embed_query`).

- [ ] **Step 1: Write the failing tests**

Append to `radis/pgsearch/tests/test_rate_limiter.py`:

```python
def test_call_with_rate_limit_returns_on_first_success(monkeypatch):
    from radis.pgsearch.utils import rate_limiter as rl

    acquire_calls = []
    result = rl.call_with_rate_limit(lambda: acquire_calls.append(1), lambda: "ok")

    assert result == "ok"
    assert len(acquire_calls) == 1


def test_call_with_rate_limit_honors_wait_and_retries(monkeypatch):
    from radis.pgsearch.utils import rate_limiter as rl

    sleep_calls = []
    monkeypatch.setattr(rl, "_sleep", lambda seconds: sleep_calls.append(seconds))

    attempts = {"n": 0}

    def flaky():
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise _make_rate_limit_error("Limit 60/min exceeded. Wait 3s.")
        return "ok"

    result = rl.call_with_rate_limit(lambda: None, flaky)

    assert result == "ok"
    assert attempts["n"] == 3
    assert sleep_calls == [3.0, 3.0]


def test_call_with_rate_limit_raises_after_max_attempts(monkeypatch):
    from radis.pgsearch.utils import rate_limiter as rl
    import openai

    monkeypatch.setattr(rl, "_sleep", lambda seconds: None)
    acquire_calls = []

    def always_fails():
        raise _make_rate_limit_error("Limit 60/min exceeded. Wait 1s.")

    with pytest.raises(openai.RateLimitError):
        rl.call_with_rate_limit(lambda: acquire_calls.append(1), always_fails, max_attempts=3)

    assert len(acquire_calls) == 3
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest radis/pgsearch/tests/test_rate_limiter.py -v -k call_with_rate_limit`
Expected: FAIL with `AttributeError: module 'radis.pgsearch.utils.rate_limiter' has no attribute 'call_with_rate_limit'`

- [ ] **Step 3: Implement `call_with_rate_limit`**

At the top of `radis/pgsearch/utils/rate_limiter.py`, change the import block (as left by Task 4):

```python
from __future__ import annotations

import re
import time
from datetime import timedelta

import openai
from django.conf import settings
from django.db import connection, transaction
from django.db.models import Sum
from django.utils import timezone

from ..models import EmbeddingRateLimitEvent

_WINDOW = timedelta(seconds=60)
```

to:

```python
from __future__ import annotations

import logging
import re
import time
from datetime import timedelta
from typing import Callable, TypeVar

import openai
from django.conf import settings
from django.db import connection, transaction
from django.db.models import Sum
from django.utils import timezone

from ..models import EmbeddingRateLimitEvent

logger = logging.getLogger(__name__)

T = TypeVar("T")

_WINDOW = timedelta(seconds=60)
```

Then append to the end of the file:

```python


def call_with_rate_limit(
    acquire_fn: Callable[[], None], fn: Callable[[], T], max_attempts: int = 3
) -> T:
    """Acquire capacity, then call `fn`. If the real gateway still rejects
    with a 429 despite our own gating (our proactive ledger is a best-effort
    estimate — see the design doc on unconfirmed cost-weighting), honor the
    server's own reported wait time and retry, up to `max_attempts`. The
    ledger entry already recorded for the failed attempt is deliberately not
    rolled back: a 429 means the real gateway is more constrained than our
    estimate, so removing our own record would only make future estimates
    more optimistic, the wrong direction."""
    for attempt in range(1, max_attempts + 1):
        acquire_fn()
        try:
            return fn()
        except openai.RateLimitError as exc:
            wait = parse_retry_after(exc)
            logger.warning(
                "embedding rate-limit gate: got 429 despite internal gating; "
                "server-reported wait=%.1fs (attempt %d/%d)",
                wait,
                attempt,
                max_attempts,
            )
            if attempt == max_attempts:
                raise
            _sleep(wait)
    raise AssertionError("unreachable: loop always returns or raises")
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest radis/pgsearch/tests/test_rate_limiter.py -v`
Expected: 12 passed

- [ ] **Step 5: Commit**

```bash
git add radis/pgsearch/utils/rate_limiter.py radis/pgsearch/tests/test_rate_limiter.py
git commit -m "feat(pgsearch): add reactive 429 handling to the rate limiter"
```

---

### Task 6: Wire the gate into `_embed_chunk_with_retry` (background bulk tier)

**Files:**
- Modify: `radis/pgsearch/tasks.py:1-17` (imports), `radis/pgsearch/tasks.py:57-65` (`_embed_chunk_with_retry`)
- Modify: `radis/pgsearch/tests/test_embed_reports_task.py`

**Interfaces:**
- Consumes: `acquire_token`, `call_with_rate_limit` (Tasks 2, 5).

- [ ] **Step 1: Write the failing wiring test**

In `radis/pgsearch/tests/test_embed_reports_task.py`, add this fixture right after the existing `caplog_tasks` fixture (after line 48, before `pytestmark = pytest.mark.django_db(transaction=True)` at line 51):

```python
@pytest.fixture(autouse=True)
def _bypass_rate_limit_gate(monkeypatch):
    """These tests exercise embed_reports_task's business logic (bisect,
    retry, logging), not the rate-limit gate itself — that's covered in
    test_rate_limiter.py. Patch it to a passthrough so these tests don't
    depend on real DB-backed gating."""
    from radis.pgsearch import tasks as tasks_module

    monkeypatch.setattr(tasks_module, "call_with_rate_limit", lambda acquire_fn, fn: fn())
```

Then add this test anywhere after the fixture definitions (e.g. right after `test_embeds_in_internal_batches`):

```python
def test_embed_chunk_with_retry_uses_rate_limit_gate(monkeypatch):
    from radis.pgsearch import tasks as tasks_module

    calls = {}

    def fake_call_with_rate_limit(acquire_fn, fn):
        calls["acquire_fn"] = acquire_fn
        return fn()

    monkeypatch.setattr(tasks_module, "call_with_rate_limit", fake_call_with_rate_limit)

    acquired = {}

    def fake_acquire_token(bucket, weight=1):
        acquired["bucket"] = bucket

    monkeypatch.setattr(tasks_module, "acquire_token", fake_acquire_token)

    fake_client = MagicMock()
    fake_client.embed_documents = MagicMock(return_value=[[0.1, 0.2]])

    result = tasks_module._embed_chunk_with_retry(fake_client, ["hello"])

    assert result == [[0.1, 0.2]]
    calls["acquire_fn"]()
    assert acquired["bucket"] == "embedding_background"
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run cli test -- -k test_embed_chunk_with_retry_uses_rate_limit_gate`
Expected: FAIL — `tasks_module` has no attribute `call_with_rate_limit` or `acquire_token` to monkeypatch (`AttributeError`), since `tasks.py` doesn't import them yet.

- [ ] **Step 3: Wire the imports and the call**

In `radis/pgsearch/tasks.py`, change the import block (lines 11-17):

```python
from .models import ReportSearchIndex
from .utils.embedding_client import (
    EmbeddingClient,
    EmbeddingClientError,
    EmbeddingPayloadTooLargeError,
)
from .utils.indexing import bulk_upsert_report_search_indexes
```

to:

```python
from .models import ReportSearchIndex
from .utils.embedding_client import (
    EmbeddingClient,
    EmbeddingClientError,
    EmbeddingPayloadTooLargeError,
)
from .utils.indexing import bulk_upsert_report_search_indexes
from .utils.rate_limiter import acquire_token, call_with_rate_limit
```

Then change `_embed_chunk_with_retry` (lines 57-65):

```python
def _embed_chunk_with_retry(client: EmbeddingClient, texts: list[str]) -> list[list[float]]:
    """Single embed call wrapped in stamina-controlled transient retries.

    Layered with Procrastinate's task-level retry: stamina handles brief
    blips (3 attempts within ~30s); Procrastinate handles extended outages
    (whole-task retry on backoff). `EmbeddingPayloadTooLargeError` is
    excluded by the predicate so the bisect logic above this layer can
    catch and resolve it without burning retry budget."""
    return client.embed_documents(texts)
```

to:

```python
def _embed_chunk_with_retry(client: EmbeddingClient, texts: list[str]) -> list[list[float]]:
    """Single embed call wrapped in stamina-controlled transient retries.

    Layered with Procrastinate's task-level retry: stamina handles brief
    blips (3 attempts within ~30s); Procrastinate handles extended outages
    (whole-task retry on backoff). `EmbeddingPayloadTooLargeError` is
    excluded by the predicate so the bisect logic above this layer can
    catch and resolve it without burning retry budget.

    Every real HTTP attempt here — including stamina retries and bisected
    sub-chunks — goes through the background-tier rate-limit gate, which
    also honors the gateway's own 429 wait time if our proactive gating
    still lets a request through that the real backend rejects."""
    return call_with_rate_limit(
        lambda: acquire_token("embedding_background"),
        lambda: client.embed_documents(texts),
    )
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run cli test -- -k test_embed_chunk_with_retry_uses_rate_limit_gate`
Expected: PASS

- [ ] **Step 5: Run the full existing test file to confirm nothing else broke**

Run: `uv run pytest radis/pgsearch/tests/test_embed_reports_task.py -v`
Expected: all tests PASS (the `_bypass_rate_limit_gate` autouse fixture keeps every other test's behavior unchanged)

- [ ] **Step 6: Commit**

```bash
git add radis/pgsearch/tasks.py radis/pgsearch/tests/test_embed_reports_task.py
git commit -m "feat(pgsearch): gate background bulk embedding through the rate limiter"
```

---

### Task 7: Wire the gate into `embed_query` (search/retrieval tier)

**Files:**
- Modify: `radis/pgsearch/utils/embedding_client.py:1-10` (imports), `radis/pgsearch/utils/embedding_client.py:151-156` (`embed_query`)
- Modify: `radis/pgsearch/tests/test_embedding_client.py`

**Interfaces:**
- Consumes: `acquire_search_priority_token`, `call_with_rate_limit` (Tasks 3, 5).

- [ ] **Step 1: Write the failing wiring test**

In `radis/pgsearch/tests/test_embedding_client.py`, add this fixture right after the `_install_transport` helper function (after its closing line, before the first `@_patched_settings()` test):

```python
@pytest.fixture(autouse=True)
def _bypass_rate_limit_gate(monkeypatch):
    """These tests exercise EmbeddingClient's request/response handling, not
    the rate-limit gate itself (covered in test_rate_limiter.py). This file
    has no django_db marker, so without this patch embed_query's real gate
    would fail with a "database access not allowed" error."""
    from radis.pgsearch.utils import embedding_client as ec

    monkeypatch.setattr(ec, "call_with_rate_limit", lambda acquire_fn, fn: fn())
```

Then add this test anywhere after that fixture (e.g. right before `test_missing_url_raises_at_construction`):

```python
@_patched_settings()
def test_embed_query_uses_rate_limit_gate(monkeypatch):
    from radis.pgsearch.utils import embedding_client as ec

    calls = {}

    def fake_call_with_rate_limit(acquire_fn, fn):
        acquire_fn()
        return fn()

    monkeypatch.setattr(ec, "call_with_rate_limit", fake_call_with_rate_limit)

    acquired = {"called": False}

    def fake_acquire_search_priority_token(weight=1):
        acquired["called"] = True

    monkeypatch.setattr(
        ec, "acquire_search_priority_token", fake_acquire_search_priority_token
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [{"embedding": [1.0, 0.0, 0.0, 0.0]}]})

    _install_transport(monkeypatch, handler)

    vec = ec.EmbeddingClient().embed_query("pneumonia")

    assert vec == [1.0, 0.0, 0.0, 0.0]
    assert acquired["called"] is True
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run cli test -- -k test_embed_query_uses_rate_limit_gate`
Expected: FAIL — `embedding_client` module has no attribute `call_with_rate_limit` or `acquire_search_priority_token` to monkeypatch (`AttributeError`).

- [ ] **Step 3: Wire the import and the call**

In `radis/pgsearch/utils/embedding_client.py`, change the import block (lines 1-8):

```python
from __future__ import annotations

import logging
import math

import httpx
import openai
from django.conf import settings

logger = logging.getLogger(__name__)
```

to:

```python
from __future__ import annotations

import logging
import math

import httpx
import openai
from django.conf import settings

from .rate_limiter import acquire_search_priority_token, call_with_rate_limit

logger = logging.getLogger(__name__)
```

Then change `embed_query` (lines 151-156):

```python
    def embed_query(self, text: str) -> list[float]:
        prefixed = f"{self._instruction}{text}" if self._instruction else text
        vectors = self.embed_documents([prefixed])
        if not vectors:
            raise EmbeddingClientError("Embedding service returned no vectors for query")
        return vectors[0]
```

to:

```python
    def embed_query(self, text: str) -> list[float]:
        prefixed = f"{self._instruction}{text}" if self._instruction else text
        vectors = call_with_rate_limit(
            acquire_search_priority_token,
            lambda: self.embed_documents([prefixed]),
        )
        if not vectors:
            raise EmbeddingClientError("Embedding service returned no vectors for query")
        return vectors[0]
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run cli test -- -k test_embed_query_uses_rate_limit_gate`
Expected: PASS

- [ ] **Step 5: Run the full existing test file to confirm nothing else broke**

Run: `uv run pytest radis/pgsearch/tests/test_embedding_client.py -v`
Expected: all tests PASS (the `_bypass_rate_limit_gate` autouse fixture keeps every other test's behavior unchanged, and none of them need real DB access)

- [ ] **Step 6: Run the full pgsearch test suite as a final sanity check**

Run: `uv run pytest radis/pgsearch/ -v`
Expected: all tests PASS

- [ ] **Step 7: Commit**

```bash
git add radis/pgsearch/utils/embedding_client.py radis/pgsearch/tests/test_embedding_client.py
git commit -m "feat(pgsearch): gate search/retrieval embedding through the rate limiter"
```
