# Shared Cross-Process 429 Backoff Implementation Plan

> **Status: REVERTED (2026-07-09).** After main landed the generic per-process
> `RateLimitGate` for LLM traffic (#242, `radis/core/utils/rate_limit.py`), the
> DB-backed `EmbeddingBackoffState` singleton was removed in favor of a
> per-process `EMBEDDING_GATE` built on that same class. Cross-process
> coordination was judged not worth the extra model/migration surface: each
> container backs off on the 429s it receives itself, and the search path is
> protected by a short per-call wait budget instead of a gate bypass. Kept for
> historical rationale only.


> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the embedding gateway's 429 backoff observable by every process sharing the database, with global exponential doubling, per `docs/superpowers/specs/2026-07-02-shared-429-backoff-design.md`.

**Architecture:** A singleton Postgres row (`EmbeddingBackoffState`, pk=1) stores `paused_until` and a global `consecutive_429s` counter. `call_with_429_backoff` gains a `shared_gate` flag: gated callers (background bulk embedding) sleep out the shared pause before every attempt and record 429s/successes; the ungated caller (search `embed_query`) records 429s but never reads the pause and keeps its local per-attempt doubling.

**Tech Stack:** Django 5.1 ORM (`select_for_update` in short transactions), pytest-django, existing `openai`/`httpx` test helpers.

## Global Constraints

- Line length 100 (Ruff); Google Python Style; `from __future__ import annotations` at top of modified modules (already present).
- `openai.RateLimitError` must stay excluded from the stamina retry predicate in `radis/pgsearch/tasks.py` (no change there — verify only).
- The final 429 after `max_attempts` must still propagate to the caller.
- No new settings, no Redis, no cache framework — Postgres only.
- Run tests from repo root: `uv run pytest radis/pgsearch/tests/test_rate_limiter.py -v` (needs the dev Postgres; if the test DB is unreachable, STOP and report rather than pointing tests at another database).
- Commit messages end with `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`.

---

### Task 1: `EmbeddingBackoffState` model + migration 0006

**Files:**
- Modify: `radis/pgsearch/models.py` (append at end)
- Create: `radis/pgsearch/migrations/0006_embeddingbackoffstate.py` (via makemigrations)
- Test: `radis/pgsearch/tests/test_rate_limiter.py` (append)

**Interfaces:**
- Produces: `radis.pgsearch.models.EmbeddingBackoffState` with fields `paused_until: datetime | None` (null=True, default None), `consecutive_429s: int` (default 0), `updated_at` (auto_now). Singleton by convention: always accessed with `pk=1`.

- [ ] **Step 1: Write the failing test**

Append to `radis/pgsearch/tests/test_rate_limiter.py`:

```python
@pytest.mark.django_db
def test_embedding_backoff_state_defaults():
    from radis.pgsearch.models import EmbeddingBackoffState

    state, created = EmbeddingBackoffState.objects.get_or_create(pk=1)

    assert created
    assert state.paused_until is None
    assert state.consecutive_429s == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest radis/pgsearch/tests/test_rate_limiter.py::test_embedding_backoff_state_defaults -v`
Expected: FAIL with `ImportError: cannot import name 'EmbeddingBackoffState'`

- [ ] **Step 3: Write the model**

Append to `radis/pgsearch/models.py`:

```python
class EmbeddingBackoffState(models.Model):
    """Singleton (always pk=1) shared reactive-backoff state for the
    embedding gateway. When any process receives a 429 it records the
    server-reported wait here; background embedding traffic in every
    container consults this row before sending, so one process's backoff
    gates them all. The counter makes repeat-429 doubling global. See
    docs/superpowers/specs/2026-07-02-shared-429-backoff-design.md."""

    paused_until = models.DateTimeField(null=True, default=None)
    consecutive_429s = models.PositiveIntegerField(default=0)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Embedding backoff state"
        verbose_name_plural = "Embedding backoff states"

    def __str__(self) -> str:
        return f"Embedding backoff state (paused_until={self.paused_until})"
```

- [ ] **Step 4: Generate the migration**

Run: `uv run python manage.py makemigrations pgsearch --name embeddingbackoffstate`
Expected: creates `radis/pgsearch/migrations/0006_embeddingbackoffstate.py` with a single `CreateModel`.

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run pytest radis/pgsearch/tests/test_rate_limiter.py::test_embedding_backoff_state_defaults -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add radis/pgsearch/models.py radis/pgsearch/migrations/0006_embeddingbackoffstate.py radis/pgsearch/tests/test_rate_limiter.py
git commit -m "feat(pgsearch): add EmbeddingBackoffState singleton for shared 429 backoff

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 2: Shared-state functions in `rate_limiter.py`

**Files:**
- Modify: `radis/pgsearch/utils/rate_limiter.py`
- Test: `radis/pgsearch/tests/test_rate_limiter.py`

**Interfaces:**
- Consumes: `EmbeddingBackoffState` from Task 1.
- Produces (all in `radis.pgsearch.utils.rate_limiter`):
  - `_now() -> datetime` — clock seam (module-level, monkeypatchable).
  - `shared_wait_seconds() -> float` — seconds until `paused_until`, `0.0` if no row / no pause / pause past.
  - `record_429(retry_after: float) -> float` — extends the shared pause (never shortens), increments the global counter, returns the wait it computed (`retry_after * 2**counter_before_increment`).
  - `record_success() -> None` — resets the counter to 0; issues no UPDATE when it is already 0.

- [ ] **Step 1: Write the failing tests**

Append to `radis/pgsearch/tests/test_rate_limiter.py`. Also add these imports at the top of the file (after the existing ones) and the fake-clock fixture — later tasks reuse both:

```python
from datetime import timedelta

from django.utils import timezone
```

```python
class _FakeClock:
    """Deterministic clock for the rate limiter's _now/_sleep seams: time
    stands still except when _sleep advances it, so pause arithmetic in
    assertions is exact."""

    def __init__(self) -> None:
        self.current = timezone.now()
        self.sleeps: list[float] = []

    def now(self):
        return self.current

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.current += timedelta(seconds=seconds)


@pytest.fixture
def clock(monkeypatch) -> _FakeClock:
    from radis.pgsearch.utils import rate_limiter as rl

    c = _FakeClock()
    monkeypatch.setattr(rl, "_now", c.now)
    monkeypatch.setattr(rl, "_sleep", c.sleep)
    return c
```

```python
@pytest.mark.django_db
def test_record_429_sets_pause_and_doubles_globally(clock):
    from radis.pgsearch.models import EmbeddingBackoffState
    from radis.pgsearch.utils import rate_limiter as rl

    assert rl.record_429(10.0) == 10.0
    assert rl.record_429(10.0) == 20.0

    state = EmbeddingBackoffState.objects.get(pk=1)
    assert state.consecutive_429s == 2
    assert state.paused_until == clock.current + timedelta(seconds=20)


@pytest.mark.django_db
def test_record_429_extends_but_never_shortens_the_pause(clock):
    from radis.pgsearch.models import EmbeddingBackoffState
    from radis.pgsearch.utils import rate_limiter as rl

    rl.record_429(30.0)
    # Second 429 with a short server wait: 1s * 2^1 = 2s candidate loses
    # to the existing 30s pause.
    rl.record_429(1.0)

    state = EmbeddingBackoffState.objects.get(pk=1)
    assert state.paused_until == clock.current + timedelta(seconds=30)
    assert state.consecutive_429s == 2


@pytest.mark.django_db
def test_shared_wait_seconds_reads_the_pause(clock):
    from radis.pgsearch.utils import rate_limiter as rl

    assert rl.shared_wait_seconds() == 0.0  # no row yet

    rl.record_429(15.0)
    assert rl.shared_wait_seconds() == 15.0

    clock.current += timedelta(seconds=20)  # pause expired
    assert rl.shared_wait_seconds() == 0.0


@pytest.mark.django_db
def test_record_success_resets_counter(clock):
    from radis.pgsearch.models import EmbeddingBackoffState
    from radis.pgsearch.utils import rate_limiter as rl

    rl.record_429(5.0)
    rl.record_success()

    state = EmbeddingBackoffState.objects.get(pk=1)
    assert state.consecutive_429s == 0
    # The pause itself is NOT cleared — it expires on its own; only the
    # doubling counter resets.
    assert state.paused_until == clock.current + timedelta(seconds=5)


@pytest.mark.django_db
def test_record_success_is_read_only_when_counter_already_zero(
    clock, django_assert_num_queries
):
    from radis.pgsearch.models import EmbeddingBackoffState
    from radis.pgsearch.utils import rate_limiter as rl

    EmbeddingBackoffState.objects.create(pk=1)

    with django_assert_num_queries(1):
        rl.record_success()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest radis/pgsearch/tests/test_rate_limiter.py -v -k "record_429 or shared_wait or record_success"`
Expected: FAIL with `AttributeError: module ... has no attribute 'record_429'` (and siblings)

- [ ] **Step 3: Implement the shared-state functions**

In `radis/pgsearch/utils/rate_limiter.py`: replace the module docstring, extend imports, and add the functions after `_sleep`. The full top of the file becomes:

```python
"""Reactive 429 handling for the embedding gateway, shared across processes.

No proactive gating: requests go straight to the gateway. But when any
process receives a 429, the server-reported wait (Retry-After header or the
"Wait Xs" phrasing in the body) is recorded in a shared singleton DB row
(EmbeddingBackoffState) that all background embedding traffic consults
before sending — one process's backoff gates every container, and repeat
429s double the wait globally. The search path deliberately bypasses the
shared pause (a user is waiting) but still records the 429s it receives.
See docs/superpowers/specs/2026-07-02-shared-429-backoff-design.md.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable
from datetime import timedelta

import openai
from django.db import transaction
from django.utils import timezone

from ..models import EmbeddingBackoffState

logger = logging.getLogger(__name__)

_STATE_PK = 1


def _now():
    """Seam so tests can inject a controllable clock instead of real time."""
    return timezone.now()


def _sleep(seconds: float) -> None:
    """Seam so tests can intercept waits instead of really blocking."""
    time.sleep(seconds)
```

(Keep `_WAIT_RE`, `_DEFAULT_RETRY_AFTER`, and `parse_retry_after` exactly as they are.) Then add:

```python
def shared_wait_seconds() -> float:
    """Seconds every gated (background) caller must still wait before
    sending, per the shared pause. 0.0 when there is no active pause."""
    state = EmbeddingBackoffState.objects.filter(pk=_STATE_PK).first()
    if state is None or state.paused_until is None:
        return 0.0
    return max((state.paused_until - _now()).total_seconds(), 0.0)


def record_429(retry_after: float) -> float:
    """Record a 429 in the shared state: extend the pause (never shorten —
    concurrent 429s take the max) and bump the global doubling counter.
    Returns the wait this 429 contributed, `retry_after * 2**counter`."""
    with transaction.atomic():
        state, _ = EmbeddingBackoffState.objects.select_for_update().get_or_create(pk=_STATE_PK)
        wait = retry_after * 2**state.consecutive_429s
        candidate = _now() + timedelta(seconds=wait)
        if state.paused_until is None or candidate > state.paused_until:
            state.paused_until = candidate
        state.consecutive_429s += 1
        state.save()
    return wait


def record_success() -> None:
    """Reset the global doubling counter after a gated call succeeded. The
    pause itself is left to expire on its own. Cheap read first so the
    steady-state happy path costs one SELECT and no writes."""
    state = EmbeddingBackoffState.objects.filter(pk=_STATE_PK).first()
    if state is None or state.consecutive_429s == 0:
        return
    EmbeddingBackoffState.objects.filter(pk=_STATE_PK).update(consecutive_429s=0)
```


- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest radis/pgsearch/tests/test_rate_limiter.py -v -k "record_429 or shared_wait or record_success"`
Expected: PASS (5 tests)

- [ ] **Step 5: Commit**

```bash
git add radis/pgsearch/utils/rate_limiter.py radis/pgsearch/tests/test_rate_limiter.py
git commit -m "feat(pgsearch): shared 429 backoff state read/write functions

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 3: Shared gate in `call_with_429_backoff`

**Files:**
- Modify: `radis/pgsearch/utils/rate_limiter.py` (replace `call_with_429_backoff`)
- Test: `radis/pgsearch/tests/test_rate_limiter.py` (update 4 existing tests, add 4)

**Interfaces:**
- Consumes: `shared_wait_seconds`, `record_429`, `record_success` from Task 2.
- Produces: `call_with_429_backoff[T](fn: Callable[[], T], max_attempts: int = 3, shared_gate: bool = True) -> T`. Behavior contract:
  - `shared_gate=True`: sleeps out the shared pause before every attempt (re-checking after each sleep); on 429 records and loops (the shared pause IS the wait); on success resets the global counter; final 429 still propagates.
  - `shared_gate=False`: never reads the pause; on 429 records to shared state, then sleeps `parse_retry_after(exc) * 2**(attempt-1)` locally (global counter must not scale a user-facing wait); success does not touch shared state.

- [ ] **Step 1: Update the four existing `call_with_429_backoff` tests**

They currently monkeypatch only `_sleep` and run without DB. The default `shared_gate=True` now touches the DB and the sleeps come from the shared pause, so rewrite them to use the `clock` fixture and `django_db`. Replace the four functions bodily:

```python
@pytest.mark.django_db
def test_call_with_429_backoff_returns_on_first_success(clock):
    from radis.pgsearch.utils import rate_limiter as rl

    result = rl.call_with_429_backoff(lambda: "ok")

    assert result == "ok"
    assert clock.sleeps == []


@pytest.mark.django_db
def test_call_with_429_backoff_waits_exponentially_then_succeeds(clock):
    """The base wait comes from the server's own hint ("Wait 3s"), lands in
    the shared pause, and doubles globally on each subsequent 429: 3s, 6s."""
    from radis.pgsearch.models import EmbeddingBackoffState
    from radis.pgsearch.utils import rate_limiter as rl

    attempts = {"n": 0}

    def flaky():
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise _make_rate_limit_error("Limit 60/min exceeded. Wait 3s.")
        return "ok"

    result = rl.call_with_429_backoff(flaky)

    assert result == "ok"
    assert attempts["n"] == 3
    assert clock.sleeps == [3.0, 6.0]
    # Success reset the global doubling counter.
    assert EmbeddingBackoffState.objects.get(pk=1).consecutive_429s == 0


@pytest.mark.django_db
def test_call_with_429_backoff_raises_after_max_attempts(clock):
    """The final 429 propagates so the caller's task-level retry policy
    applies — but it is still recorded in the shared state first, so other
    processes back off even though this caller gave up."""
    from radis.pgsearch.models import EmbeddingBackoffState
    from radis.pgsearch.utils import rate_limiter as rl

    attempts = {"n": 0}

    def always_fails():
        attempts["n"] += 1
        raise _make_rate_limit_error("Limit 60/min exceeded. Wait 1s.")

    with pytest.raises(openai.RateLimitError):
        rl.call_with_429_backoff(always_fails, max_attempts=3)

    assert attempts["n"] == 3
    assert clock.sleeps == [1.0, 2.0]
    assert EmbeddingBackoffState.objects.get(pk=1).consecutive_429s == 3


@pytest.mark.django_db
def test_call_with_429_backoff_does_not_intercept_other_errors(clock):
    """Only 429s are backed off here — other errors propagate immediately
    to the stamina/Procrastinate layers."""
    from radis.pgsearch.utils import rate_limiter as rl

    def fails():
        raise ValueError("not a 429")

    with pytest.raises(ValueError):
        rl.call_with_429_backoff(fails)

    assert clock.sleeps == []
```

- [ ] **Step 2: Add the four new behavior tests**

```python
@pytest.mark.django_db
def test_shared_gate_sleeps_out_preexisting_pause(clock):
    """A pause created by ANOTHER process (here: a direct record_429) gates
    this caller before its first attempt."""
    from radis.pgsearch.utils import rate_limiter as rl

    rl.record_429(7.0)

    result = rl.call_with_429_backoff(lambda: "ok")

    assert result == "ok"
    assert clock.sleeps == [7.0]


@pytest.mark.django_db
def test_shared_gate_rechecks_pause_extended_during_sleep(clock, monkeypatch):
    """If another process extends the pause while we sleep, we sleep again
    instead of firing into a known-throttled gateway."""
    from radis.pgsearch.utils import rate_limiter as rl

    rl.record_429(5.0)

    original_sleep = clock.sleep
    extended = {"done": False}

    def sleep_and_extend(seconds: float) -> None:
        original_sleep(seconds)
        if not extended["done"]:
            extended["done"] = True
            rl.record_429(4.0)  # concurrent 429 elsewhere: pause += 4*2^1 = 8s

    monkeypatch.setattr(rl, "_sleep", sleep_and_extend)

    result = rl.call_with_429_backoff(lambda: "ok")

    assert result == "ok"
    assert clock.sleeps == [5.0, 8.0]


@pytest.mark.django_db
def test_ungated_search_path_skips_pause_but_records_its_429(clock):
    """shared_gate=False (search): never waits on the shared pause, but a
    429 it receives still informs the shared state for everyone else."""
    from radis.pgsearch.models import EmbeddingBackoffState
    from radis.pgsearch.utils import rate_limiter as rl

    rl.record_429(60.0)  # big pause from background traffic

    attempts = {"n": 0}

    def flaky():
        attempts["n"] += 1
        if attempts["n"] < 2:
            raise _make_rate_limit_error("Limit 60/min exceeded. Wait 3s.")
        return "ok"

    result = rl.call_with_429_backoff(flaky, shared_gate=False)

    assert result == "ok"
    # Slept only its own local 3s — not the 60s shared pause.
    assert clock.sleeps == [3.0]
    # ...but its 429 bumped the global counter.
    assert EmbeddingBackoffState.objects.get(pk=1).consecutive_429s == 2


@pytest.mark.django_db
def test_ungated_local_wait_not_scaled_by_global_counter(clock):
    """Bulk traffic may drive the global counter high; a user-facing search
    retry must still wait only the server's own hint (local doubling)."""
    from radis.pgsearch.utils import rate_limiter as rl

    rl.record_429(5.0)
    rl.record_429(5.0)  # global counter now 2

    attempts = {"n": 0}

    def flaky():
        attempts["n"] += 1
        if attempts["n"] < 2:
            raise _make_rate_limit_error("Limit 60/min exceeded. Wait 3s.")
        return "ok"

    result = rl.call_with_429_backoff(flaky, shared_gate=False)

    assert result == "ok"
    assert clock.sleeps == [3.0]  # NOT 3 * 2^2
```

- [ ] **Step 3: Run tests to verify the new/updated ones fail**

Run: `uv run pytest radis/pgsearch/tests/test_rate_limiter.py -v`
Expected: the Task 1/2 tests and `parse_retry_after` tests PASS; the updated/new `call_with_429_backoff` tests FAIL (`TypeError: ... unexpected keyword argument 'shared_gate'` or wrong sleep values).

- [ ] **Step 4: Replace `call_with_429_backoff`**

```python
def call_with_429_backoff[T](
    fn: Callable[[], T], max_attempts: int = 3, shared_gate: bool = True
) -> T:
    """Call `fn`; on a 429 wait and retry, up to `max_attempts`, with the
    wait shared across processes via EmbeddingBackoffState.

    shared_gate=True (background bulk embedding): sleep out the shared
    pause before every attempt, record 429s (extending the pause with
    global doubling) and successes (resetting the doubling counter). The
    shared pause is the only wait mechanism — no separate local sleep.

    shared_gate=False (search/retrieval, a user is waiting): never reads
    the shared pause; a 429 still gets recorded so background traffic
    learns from it, but the local retry wait is the server's own hint with
    per-attempt doubling, unscaled by the global counter. The final 429
    always propagates so the caller's own retry/error policy applies."""
    for attempt in range(1, max_attempts + 1):
        if shared_gate:
            while (wait := shared_wait_seconds()) > 0:
                logger.info(
                    "embedding shared backoff: waiting %.1fs before sending", wait
                )
                _sleep(wait)
        try:
            result = fn()
        except openai.RateLimitError as exc:
            retry_after = parse_retry_after(exc)
            shared_wait = record_429(retry_after)
            if attempt == max_attempts:
                raise
            if shared_gate:
                logger.warning(
                    "embedding 429: shared pause extended by %.1fs (attempt %d/%d)",
                    shared_wait,
                    attempt,
                    max_attempts,
                )
            else:
                local_wait = retry_after * 2 ** (attempt - 1)
                logger.warning(
                    "embedding 429 (ungated): waiting %.1fs before retry (attempt %d/%d)",
                    local_wait,
                    attempt,
                    max_attempts,
                )
                _sleep(local_wait)
            continue
        if shared_gate:
            record_success()
        return result
    raise AssertionError("unreachable: loop always returns or raises")
```

- [ ] **Step 5: Run the whole rate limiter test file**

Run: `uv run pytest radis/pgsearch/tests/test_rate_limiter.py -v`
Expected: ALL PASS

- [ ] **Step 6: Commit**

```bash
git add radis/pgsearch/utils/rate_limiter.py radis/pgsearch/tests/test_rate_limiter.py
git commit -m "feat(pgsearch): gate 429 backoff through shared cross-process state

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 4: Wire the search path, fix test doubles, full verification

**Files:**
- Modify: `radis/pgsearch/utils/embedding_client.py:100`
- Modify: `radis/pgsearch/tests/test_embedding_client.py:35-41, 233-242`
- Modify: `radis/pgsearch/tests/test_embed_reports_task.py:51-58, 132-141`
- Test: whole `radis/pgsearch` suite + lint

**Interfaces:**
- Consumes: `call_with_429_backoff(fn, max_attempts=3, shared_gate=...)` from Task 3.
- Produces: `embed_query` passes `shared_gate=False`; `_embed_chunk_with_retry` in `tasks.py` stays as-is (gated by default — verify, don't change).

- [ ] **Step 1: Update the `embed_query` assertion test first (TDD)**

In `radis/pgsearch/tests/test_embedding_client.py`, `test_embed_query_uses_429_backoff` (line ~233): make the fake accept and assert the flag.

```python
    def fake_call_with_429_backoff(fn, **kwargs):
        called["n"] += 1
        assert kwargs.get("shared_gate") is False, (
            "embed_query must bypass the shared pause (a user is waiting) "
            "while still recording its own 429s"
        )
        return fn()
```

(Keep the rest of the test as it is.)

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest radis/pgsearch/tests/test_embedding_client.py::test_embed_query_uses_429_backoff -v`
Expected: FAIL on the `shared_gate` assertion (kwarg absent).

- [ ] **Step 3: Make both bypass fixtures kwargs-tolerant**

`radis/pgsearch/tests/test_embedding_client.py` line ~41 and `radis/pgsearch/tests/test_embed_reports_task.py` line ~58 — the passthrough doubles currently take exactly one positional arg and will crash on the new kwarg:

```python
    monkeypatch.setattr(ec, "call_with_429_backoff", lambda fn, **kwargs: fn())
```

```python
    monkeypatch.setattr(tasks_module, "call_with_429_backoff", lambda fn, **kwargs: fn())
```

Also `test_embed_chunk_with_retry_wraps_call_in_429_backoff` in `test_embed_reports_task.py` (~line 137): change its fake's signature to `def fake_call_with_429_backoff(fn, **kwargs):` and keep its body.

- [ ] **Step 4: Wire `embed_query`**

`radis/pgsearch/utils/embedding_client.py:100`:

```python
        vectors = call_with_429_backoff(
            lambda: self.embed_documents([prefixed]), shared_gate=False
        )
```

- [ ] **Step 5: Run the full pgsearch suite**

Run: `uv run pytest radis/pgsearch/ -v`
Expected: ALL PASS. If any test fails on unexpected DB access (`RuntimeError: Database access not allowed`), that test now exercises `record_429` for real — give it `@pytest.mark.django_db` only if it genuinely should hit shared state; prefer the kwargs-tolerant passthrough fixture otherwise.

- [ ] **Step 6: Lint**

Run: `uv run cli lint`
Expected: clean (fix any line-length findings in the files touched).

- [ ] **Step 7: Commit**

```bash
git add radis/pgsearch/utils/embedding_client.py radis/pgsearch/tests/test_embedding_client.py radis/pgsearch/tests/test_embed_reports_task.py
git commit -m "feat(pgsearch): search embedding bypasses shared pause but records 429s

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```
