# Embedding Client on the OpenAI SDK Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Migrate `radis.pgsearch.utils.embedding_client.EmbeddingClient` from a hand-rolled `httpx.Client` + dual-backend (`OpenAIBackend` / `OllamaBackend`) layout to the `openai` SDK with a single OpenAI-compatible endpoint, replacing substring-matched payload-too-large detection with structured `error.code` matching, and surfacing typed `openai.OpenAIError` subclasses so a future rate-limit gate can compose cleanly.

**Architecture:** One sync `EmbeddingClient` wrapping `openai.OpenAI` (the async variant is deliberately out of scope — both call sites are sync; ADRF is not being adopted). Matryoshka truncation + L2 normalization happen post-parse on the SDK's `CreateEmbeddingResponse.data[*].embedding`. `EmbeddingPayloadTooLargeError` is raised from a single classifier that reads `BadRequestError.body["error"]["code"]` against `{"context_length_exceeded", "string_above_max_length"}`, plus a 413 catch via `APIStatusError`. A Django system check (`pgsearch.E002` / `E003`) fails startup if either of the removed env vars (`EMBEDDING_BACKEND`, `EMBEDDING_PROVIDER_PATH`) is still set.

**Tech Stack:** Python 3.12, Django 5.1, `openai>=1.64.0` (already a dep, used by `ChatClient`), `httpx.MockTransport` for SDK transport injection in tests, pytest + pytest-django, stamina (existing retry layer), Procrastinate (task queue).

**Spec:** `docs/superpowers/specs/2026-06-30-embedding-client-openai-sdk-design.md`

## Global Constraints

- Wire-format compatibility preserved: existing `ReportSearchIndex.embedding` rows must remain valid. No DB migration, no re-embedding.
- No new dependencies; `openai` is already in `pyproject.toml`.
- Sync only. Do not add `AsyncEmbeddingClient` or `openai.AsyncOpenAI`.
- Typed `openai.OpenAIError` subclasses must NOT be wrapped in `EmbeddingClientError` (callers depend on the typed exceptions to discriminate; see spec §"Errors").
- Stamina retry predicate must explicitly exclude `openai.RateLimitError` so a future rate-limit gate can intercept it without stamina swallowing the 429 first.
- Project line length is 100 chars (Ruff); 120 for templates. Lazy `%s` formatting in `logger.*` calls (no f-strings) — matches existing pgsearch convention.
- Code style: Google Python Style Guide, type hints throughout, pyright basic-mode clean.

## File map

| File | Role |
|---|---|
| `radis/pgsearch/utils/embedding_client.py` | Rewrite: single `EmbeddingClient` over `openai.OpenAI`; structured 4xx classifier; drop `EmbeddingBackend` / `OpenAIBackend` / `OllamaBackend` / `BACKENDS` / `_TOO_LARGE_MARKERS` / `_is_payload_too_large` / `_resolve_config`. Keep `_l2_normalize` / `_normalize_response` / error classes. |
| `radis/pgsearch/apps.py` | Add Django system check `check_legacy_embedding_vars` registering `pgsearch.E002` / `E003`. |
| `radis/pgsearch/tasks.py:25-34` | Widen `_is_retryable_embedding_error`: also retry `openai.APIConnectionError`, `openai.APITimeoutError`, `openai.InternalServerError`. Continue excluding `EmbeddingPayloadTooLargeError` and explicitly exclude `openai.RateLimitError`. |
| `radis/pgsearch/providers.py:111,224` | Widen `except EmbeddingClientError` → `except (EmbeddingClientError, openai.OpenAIError)` so transient SDK errors on the read path still trigger the FTS fallback. |
| `radis/settings/base.py:340-342` | Remove `EMBEDDING_BACKEND` and `EMBEDDING_PROVIDER_PATH` settings. |
| `example.env:137-169` | Replace the dual-backend block with a single OpenAI-compatible recipe + documented Ollama variant (`/v1` suffix). |
| `radis/pgsearch/tests/test_embedding_client.py` | Rewrite to target the SDK-based client; drop backend-specific tests; replace marker-list parametrized tests with code-based tests. |
| `radis/pgsearch/tests/test_apps_checks.py` | Add tests for `pgsearch.E002` / `E003` legacy-env-var detection. |
| `radis/pgsearch/tests/test_embed_reports_task.py` | Touch only if any test injects `httpx`-typed errors — none today; this file passes unchanged. (Verification step.) |
| `radis/pgsearch/tests/test_provider_hybrid.py` | Touch only if any test asserts on the exception class caught by the FTS fallback. (Verification step.) |

---

### Task 1: Add system check for removed env vars

**Files:**
- Modify: `radis/pgsearch/apps.py`
- Modify: `radis/pgsearch/tests/test_apps_checks.py`

**Interfaces:**
- Consumes: nothing.
- Produces: a Django system check that calls `Error(...)` with `id="pgsearch.E002"` when `os.environ["EMBEDDING_BACKEND"]` is set, and `id="pgsearch.E003"` when `os.environ["EMBEDDING_PROVIDER_PATH"]` is set. Runs on every `manage.py` invocation.

This ships first so that as soon as Task 5 removes the settings, any deployment still carrying the legacy `.env` lines surfaces the migration message instead of silently dropping the configuration.

- [ ] **Step 1: Read the current apps.py to find the existing check registration site**

Run: `cat radis/pgsearch/apps.py`
Expected: at minimum a `PgSearchConfig(AppConfig)` class; likely an existing `@register()` system check (used in `test_apps_checks.py`).

- [ ] **Step 2: Write the failing tests**

Edit `radis/pgsearch/tests/test_apps_checks.py`. Append:

```python
import os
from unittest.mock import patch

from radis.pgsearch.apps import check_legacy_embedding_vars


def test_legacy_embedding_backend_var_raises_e002():
    with patch.dict(os.environ, {"EMBEDDING_BACKEND": "openai"}, clear=False):
        errors = check_legacy_embedding_vars(app_configs=None)
    assert any(e.id == "pgsearch.E002" for e in errors)


def test_legacy_embedding_provider_path_var_raises_e003():
    with patch.dict(os.environ, {"EMBEDDING_PROVIDER_PATH": "/api/embeddings"}, clear=False):
        errors = check_legacy_embedding_vars(app_configs=None)
    assert any(e.id == "pgsearch.E003" for e in errors)


def test_no_errors_when_legacy_vars_absent(monkeypatch):
    monkeypatch.delenv("EMBEDDING_BACKEND", raising=False)
    monkeypatch.delenv("EMBEDDING_PROVIDER_PATH", raising=False)
    assert check_legacy_embedding_vars(app_configs=None) == []
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `uv run pytest radis/pgsearch/tests/test_apps_checks.py -v -k legacy`
Expected: ImportError on `check_legacy_embedding_vars` (function does not exist yet).

- [ ] **Step 4: Add the check function and register it**

Edit `radis/pgsearch/apps.py`. Add at module level (alongside the existing imports / checks):

```python
import os

from django.core.checks import Error, register


@register()
def check_legacy_embedding_vars(app_configs, **kwargs):
    """Fail startup if the deployment still carries env vars removed in the
    OpenAI-SDK migration (see docs/superpowers/specs/2026-06-30-embedding-
    client-openai-sdk-design.md). A silent ignore would let a misconfigured
    `.env` produce subtly wrong embedding-service URLs."""
    errors = []
    if os.environ.get("EMBEDDING_BACKEND"):
        errors.append(
            Error(
                "EMBEDDING_BACKEND is no longer supported; remove it from .env. "
                "All embedding providers now use the OpenAI-compatible "
                "/v1/embeddings endpoint via the openai SDK.",
                id="pgsearch.E002",
            )
        )
    if os.environ.get("EMBEDDING_PROVIDER_PATH"):
        errors.append(
            Error(
                "EMBEDDING_PROVIDER_PATH is no longer supported; append the path "
                "to EMBEDDING_PROVIDER_URL instead "
                "(e.g. http://host:11434/v1).",
                id="pgsearch.E003",
            )
        )
    return errors
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest radis/pgsearch/tests/test_apps_checks.py -v`
Expected: all tests pass, including the three new ones.

- [ ] **Step 6: Commit**

```bash
git add radis/pgsearch/apps.py radis/pgsearch/tests/test_apps_checks.py
git commit -m "feat(pgsearch): system check for removed EMBEDDING_BACKEND/PATH env vars"
```

---

### Task 2: Rewrite EmbeddingClient on the openai SDK

**Files:**
- Modify: `radis/pgsearch/utils/embedding_client.py` (rewrite ~210 → ~140 lines)
- Modify: `radis/pgsearch/tests/test_embedding_client.py` (rewrite)

**Interfaces:**
- Consumes: `openai>=1.64.0` (already in pyproject.toml), `settings.EMBEDDING_PROVIDER_URL`, `settings.EMBEDDING_PROVIDER_API_KEY`, `settings.EMBEDDING_MODEL_NAME`, `settings.EMBEDDING_DIM`, `settings.EMBEDDING_REQUEST_TIMEOUT`, `settings.EMBEDDING_QUERY_INSTRUCTION`.
- Produces:
  - `EmbeddingClient` with `embed_documents(texts: list[str]) -> list[list[float]]`, `embed_query(text: str) -> list[float]`, `close() -> None`, `__enter__() -> EmbeddingClient`, `__exit__(...) -> None`.
  - `EmbeddingClientError(Exception)`, `EmbeddingPayloadTooLargeError(EmbeddingClientError)`.
  - Module-level seam `_build_http_client() -> httpx.Client` so tests can swap in `httpx.MockTransport` by monkey-patching this symbol. The returned `httpx.Client` is passed to `openai.OpenAI(http_client=...)`.

- [ ] **Step 1: Write the new test file**

Replace the contents of `radis/pgsearch/tests/test_embedding_client.py`:

```python
import json

import httpx
import pytest
from django.test import override_settings


def _patched_settings():
    """Decorator factory: returns a single override_settings with the minimal
    config the new SDK-based client needs. Drops EMBEDDING_BACKEND and
    EMBEDDING_PROVIDER_PATH (removed in this migration)."""
    return override_settings(
        EMBEDDING_PROVIDER_URL="http://embed.example/v1",
        EMBEDDING_PROVIDER_API_KEY="secret",
        EMBEDDING_MODEL_NAME="qwen3",
        EMBEDDING_DIM=4,
        EMBEDDING_REQUEST_TIMEOUT=10,
        EMBEDDING_QUERY_INSTRUCTION="INST: ",
    )


def _install_transport(monkeypatch, handler):
    """Swap in an httpx.MockTransport via the module's _build_http_client seam.
    The returned client gets passed to openai.OpenAI(http_client=...)."""
    from radis.pgsearch.utils import embedding_client as ec

    monkeypatch.setattr(
        ec,
        "_build_http_client",
        lambda: httpx.Client(transport=httpx.MockTransport(handler)),
    )


@_patched_settings()
def test_embed_documents_posts_payload_and_normalizes(monkeypatch):
    from radis.pgsearch.utils import embedding_client as ec

    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "object": "list",
                "data": [{"object": "embedding", "index": 0,
                          "embedding": [3.0, 0.0, 0.0, 4.0]}],
                "model": "qwen3",
                "usage": {"prompt_tokens": 1, "total_tokens": 1},
            },
        )

    _install_transport(monkeypatch, handler)
    vectors = ec.EmbeddingClient().embed_documents(["hello"])

    assert seen["url"] == "http://embed.example/v1/embeddings"
    assert seen["auth"] == "Bearer secret"
    assert seen["body"] == {"model": "qwen3", "input": ["hello"]}
    # L2-normalize: original norm = 5 -> [0.6, 0, 0, 0.8].
    assert len(vectors) == 1
    assert vectors[0] == pytest.approx([0.6, 0.0, 0.0, 0.8])


@override_settings(
    EMBEDDING_PROVIDER_URL="http://embed.example/v1",
    EMBEDDING_PROVIDER_API_KEY="",
    EMBEDDING_MODEL_NAME="qwen3",
    EMBEDDING_DIM=2,
    EMBEDDING_REQUEST_TIMEOUT=10,
    EMBEDDING_QUERY_INSTRUCTION="INST: ",
)
def test_embed_query_prepends_instruction(monkeypatch):
    from radis.pgsearch.utils import embedding_client as ec

    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "object": "list",
                "data": [{"object": "embedding", "index": 0,
                          "embedding": [1.0, 0.0]}],
                "model": "qwen3",
                "usage": {"prompt_tokens": 1, "total_tokens": 1},
            },
        )

    _install_transport(monkeypatch, handler)
    ec.EmbeddingClient().embed_query("hello")
    assert seen["body"]["input"] == ["INST: hello"]


@override_settings(
    EMBEDDING_PROVIDER_URL="http://embed.example/v1",
    EMBEDDING_PROVIDER_API_KEY="",
    EMBEDDING_MODEL_NAME="qwen3",
    EMBEDDING_DIM=2,
    EMBEDDING_REQUEST_TIMEOUT=10,
    EMBEDDING_QUERY_INSTRUCTION="",
)
def test_dim_too_small_raises(monkeypatch):
    from radis.pgsearch.utils import embedding_client as ec

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "object": "list",
                "data": [{"object": "embedding", "index": 0, "embedding": [1.0]}],
                "model": "qwen3",
                "usage": {"prompt_tokens": 1, "total_tokens": 1},
            },
        )

    _install_transport(monkeypatch, handler)
    with pytest.raises(ec.EmbeddingClientError):
        ec.EmbeddingClient().embed_documents(["x"])


@override_settings(
    EMBEDDING_PROVIDER_URL="http://embed.example/v1",
    EMBEDDING_PROVIDER_API_KEY="",
    EMBEDDING_MODEL_NAME="qwen3",
    EMBEDDING_DIM=2,
    EMBEDDING_REQUEST_TIMEOUT=10,
    EMBEDDING_QUERY_INSTRUCTION="",
)
def test_oversized_embedding_truncates_and_renormalizes(monkeypatch):
    from radis.pgsearch.utils import embedding_client as ec

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "object": "list",
                "data": [{"object": "embedding", "index": 0,
                          "embedding": [3.0, 4.0, 99.0, 99.0]}],
                "model": "qwen3",
                "usage": {"prompt_tokens": 1, "total_tokens": 1},
            },
        )

    _install_transport(monkeypatch, handler)
    vectors = ec.EmbeddingClient().embed_documents(["x"])
    assert vectors[0] == pytest.approx([0.6, 0.8])


@override_settings(
    EMBEDDING_PROVIDER_URL="http://embed.example/v1",
    EMBEDDING_PROVIDER_API_KEY="",
    EMBEDDING_MODEL_NAME="qwen3",
    EMBEDDING_DIM=2,
    EMBEDDING_REQUEST_TIMEOUT=10,
    EMBEDDING_QUERY_INSTRUCTION="",
)
def test_5xx_propagates_as_typed_openai_error(monkeypatch):
    """5xx must surface as openai.InternalServerError (not wrapped) so the
    stamina retry predicate in tasks.py can match on the typed class."""
    import openai

    from radis.pgsearch.utils import embedding_client as ec

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="service unavailable")

    _install_transport(monkeypatch, handler)
    with pytest.raises(openai.InternalServerError):
        ec.EmbeddingClient().embed_documents(["x"])


@override_settings(
    EMBEDDING_PROVIDER_URL="http://embed.example/v1",
    EMBEDDING_PROVIDER_API_KEY="",
    EMBEDDING_MODEL_NAME="qwen3",
    EMBEDDING_DIM=2,
    EMBEDDING_REQUEST_TIMEOUT=10,
    EMBEDDING_QUERY_INSTRUCTION="",
)
def test_429_propagates_as_typed_rate_limit_error(monkeypatch):
    """429 must surface as openai.RateLimitError (not wrapped) so a future
    rate-limit gate can intercept it."""
    import openai

    from radis.pgsearch.utils import embedding_client as ec

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"error": {"message": "slow down"}})

    _install_transport(monkeypatch, handler)
    with pytest.raises(openai.RateLimitError):
        ec.EmbeddingClient().embed_documents(["x"])


@override_settings(
    EMBEDDING_PROVIDER_URL="http://embed.example/v1",
    EMBEDDING_PROVIDER_API_KEY="",
    EMBEDDING_MODEL_NAME="qwen3",
    EMBEDDING_DIM=2,
    EMBEDDING_REQUEST_TIMEOUT=10,
    EMBEDDING_QUERY_INSTRUCTION="",
)
def test_too_large_via_413(monkeypatch):
    from radis.pgsearch.utils import embedding_client as ec

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(413, text="payload too large")

    _install_transport(monkeypatch, handler)
    with pytest.raises(ec.EmbeddingPayloadTooLargeError):
        ec.EmbeddingClient().embed_documents(["x"])


@override_settings(
    EMBEDDING_PROVIDER_URL="http://embed.example/v1",
    EMBEDDING_PROVIDER_API_KEY="",
    EMBEDDING_MODEL_NAME="qwen3",
    EMBEDDING_DIM=2,
    EMBEDDING_REQUEST_TIMEOUT=10,
    EMBEDDING_QUERY_INSTRUCTION="",
)
def test_too_large_via_400_with_context_length_exceeded_code(monkeypatch):
    from radis.pgsearch.utils import embedding_client as ec

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={"error": {"message": "this is too long",
                            "code": "context_length_exceeded"}},
        )

    _install_transport(monkeypatch, handler)
    with pytest.raises(ec.EmbeddingPayloadTooLargeError):
        ec.EmbeddingClient().embed_documents(["x"])


@override_settings(
    EMBEDDING_PROVIDER_URL="http://embed.example/v1",
    EMBEDDING_PROVIDER_API_KEY="",
    EMBEDDING_MODEL_NAME="qwen3",
    EMBEDDING_DIM=2,
    EMBEDDING_REQUEST_TIMEOUT=10,
    EMBEDDING_QUERY_INSTRUCTION="",
)
def test_too_large_via_400_with_string_above_max_length_code(monkeypatch):
    from radis.pgsearch.utils import embedding_client as ec

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={"error": {"message": "too long",
                            "code": "string_above_max_length"}},
        )

    _install_transport(monkeypatch, handler)
    with pytest.raises(ec.EmbeddingPayloadTooLargeError):
        ec.EmbeddingClient().embed_documents(["x"])


@override_settings(
    EMBEDDING_PROVIDER_URL="http://embed.example/v1",
    EMBEDDING_PROVIDER_API_KEY="",
    EMBEDDING_MODEL_NAME="qwen3",
    EMBEDDING_DIM=2,
    EMBEDDING_REQUEST_TIMEOUT=10,
    EMBEDDING_QUERY_INSTRUCTION="",
)
def test_400_with_other_code_is_not_too_large(monkeypatch):
    """400 with a non-context-length error code surfaces as
    EmbeddingClientError (not the too-large subclass)."""
    from radis.pgsearch.utils import embedding_client as ec

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={"error": {"message": "missing field", "code": "invalid_request"}},
        )

    _install_transport(monkeypatch, handler)
    with pytest.raises(ec.EmbeddingClientError) as excinfo:
        ec.EmbeddingClient().embed_documents(["x"])
    assert not isinstance(excinfo.value, ec.EmbeddingPayloadTooLargeError)


@override_settings(
    EMBEDDING_PROVIDER_URL="http://embed.example/v1",
    EMBEDDING_PROVIDER_API_KEY="",
    EMBEDDING_MODEL_NAME="qwen3",
    EMBEDDING_DIM=2,
    EMBEDDING_REQUEST_TIMEOUT=10,
    EMBEDDING_QUERY_INSTRUCTION="",
)
def test_400_with_no_error_object_is_not_too_large(monkeypatch):
    """Non-OpenAI-shaped 400 (no error.code) must NOT be classified as too-large
    — bisecting on the wrong error is worse than not bisecting."""
    from radis.pgsearch.utils import embedding_client as ec

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, text="plain text 400 from non-conforming provider")

    _install_transport(monkeypatch, handler)
    with pytest.raises(ec.EmbeddingClientError) as excinfo:
        ec.EmbeddingClient().embed_documents(["x"])
    assert not isinstance(excinfo.value, ec.EmbeddingPayloadTooLargeError)


@override_settings(
    EMBEDDING_PROVIDER_URL="",
    EMBEDDING_PROVIDER_API_KEY="",
    EMBEDDING_MODEL_NAME="qwen3",
    EMBEDDING_DIM=2,
    EMBEDDING_REQUEST_TIMEOUT=10,
    EMBEDDING_QUERY_INSTRUCTION="",
)
def test_missing_url_raises_at_construction():
    from radis.pgsearch.utils import embedding_client as ec

    with pytest.raises(ec.EmbeddingClientError, match="EMBEDDING_PROVIDER_URL"):
        ec.EmbeddingClient()


@override_settings(
    EMBEDDING_PROVIDER_URL="http://embed.example/v1",
    EMBEDDING_PROVIDER_API_KEY="",
    EMBEDDING_MODEL_NAME="qwen3",
    EMBEDDING_DIM=2,
    EMBEDDING_REQUEST_TIMEOUT=10,
    EMBEDDING_QUERY_INSTRUCTION="",
)
def test_context_manager_closes_underlying_http_client(monkeypatch):
    from radis.pgsearch.utils import embedding_client as ec

    closed = {"value": False}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "object": "list", "data": [], "model": "qwen3",
            "usage": {"prompt_tokens": 0, "total_tokens": 0}})

    real_client = httpx.Client(transport=httpx.MockTransport(handler))
    original_close = real_client.close

    def tracking_close():
        closed["value"] = True
        original_close()

    real_client.close = tracking_close  # type: ignore[method-assign]
    monkeypatch.setattr(ec, "_build_http_client", lambda: real_client)

    with ec.EmbeddingClient():
        pass
    assert closed["value"] is True
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest radis/pgsearch/tests/test_embedding_client.py -v`
Expected: every test fails with ImportError (`BACKENDS`, `OllamaBackend`, `OpenAIBackend` no longer importable) OR AttributeError on the new symbols that don't exist yet.

- [ ] **Step 3: Replace the embedding client implementation**

Replace the entire contents of `radis/pgsearch/utils/embedding_client.py`:

```python
from __future__ import annotations

import logging
import math

import httpx
import openai
from django.conf import settings

logger = logging.getLogger(__name__)


class EmbeddingClientError(Exception):
    """Raised when the embedding service returns an error or a malformed response,
    or when configuration is invalid. Typed `openai.OpenAIError` subclasses
    (RateLimitError, BadRequestError, InternalServerError, ...) are NOT wrapped
    in this class — callers that want to discriminate (stamina retry predicate,
    future rate-limit gate) match on the SDK types directly."""


class EmbeddingPayloadTooLargeError(EmbeddingClientError):
    """Raised when the backend rejects a request because one or more inputs
    exceed the model's context window. Callers can bisect the batch and
    retry — `embed_reports_task` does exactly that."""


# Stable, structured error codes the OpenAI-compat ecosystem returns when an
# input exceeds the model context. The OpenAI SDK exposes these on
# `BadRequestError.body["error"]["code"]`. Substring-matching the human-readable
# message (the previous approach) drifted on provider version bumps and
# false-positived on unrelated 4xx; the structured code does not.
_TOO_LARGE_ERROR_CODES = frozenset({"context_length_exceeded", "string_above_max_length"})


def _build_http_client() -> httpx.Client:
    """Indirection so tests can swap in an httpx.MockTransport. The returned
    client is passed to openai.OpenAI(http_client=...)."""
    return httpx.Client(timeout=settings.EMBEDDING_REQUEST_TIMEOUT)


def _l2_normalize(vec: list[float]) -> list[float]:
    norm = math.sqrt(sum(x * x for x in vec))
    if norm == 0.0:
        return vec
    return [x / norm for x in vec]


def _normalize_response(
    raw: list[list[float]], expected_count: int, target_dim: int
) -> list[list[float]]:
    if len(raw) != expected_count:
        raise EmbeddingClientError(
            f"Embedding count mismatch: requested {expected_count}, "
            f"backend returned {len(raw)}"
        )
    normalized: list[list[float]] = []
    for vec in raw:
        if len(vec) < target_dim:
            raise EmbeddingClientError(
                f"Embedding dim too small: got {len(vec)}, expected at least {target_dim}"
            )
        if len(vec) > target_dim:
            # Matryoshka truncation: keep first EMBEDDING_DIM components, then renormalize.
            # Qwen3-Embedding is trained to retain quality at truncated dimensions.
            normalized.append(_l2_normalize(list(vec[:target_dim])))
        else:
            # Length already matches; still normalize since we can't assume
            # all providers return unit vectors.
            normalized.append(_l2_normalize(list(vec)))
    return normalized


def _classify_too_large(exc: openai.BadRequestError) -> EmbeddingClientError:
    """Map a BadRequestError to either the too-large subclass or the base
    error, based on the structured error.code. Non-OpenAI-shaped bodies
    (no `error.code`) are deliberately treated as NOT too-large — bisecting
    on the wrong error is worse than not bisecting on a real one."""
    body = getattr(exc, "body", None)
    code: str | None = None
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict):
            raw_code = err.get("code")
            if isinstance(raw_code, str):
                code = raw_code
    snippet = str(exc)[:200]
    if code in _TOO_LARGE_ERROR_CODES:
        return EmbeddingPayloadTooLargeError(
            f"Embedding service rejected payload as too large (code={code}): {snippet}"
        )
    return EmbeddingClientError(f"Embedding service returned 400: {snippet}")


class EmbeddingClient:
    """Sync embedding client over the openai SDK. Single OpenAI-compatible
    endpoint (set EMBEDDING_PROVIDER_URL to end in /v1). Same shape for OpenAI,
    Azure, vLLM, an LLM gateway, or Ollama's /v1 compatibility layer."""

    def __init__(self) -> None:
        base_url = settings.EMBEDDING_PROVIDER_URL
        if not base_url:
            raise EmbeddingClientError("EMBEDDING_PROVIDER_URL is not configured")
        # SDK rejects empty api_key at construction; "unused" is the documented
        # placeholder for self-hosted endpoints that ignore auth (Ollama, vLLM).
        api_key = settings.EMBEDDING_PROVIDER_API_KEY or "unused"
        self._http = _build_http_client()
        self._client = openai.OpenAI(
            base_url=base_url,
            api_key=api_key,
            http_client=self._http,
            max_retries=0,  # surface 429 immediately so a future gate can arm
            timeout=settings.EMBEDDING_REQUEST_TIMEOUT,
        )
        self._model = settings.EMBEDDING_MODEL_NAME
        self._dim = settings.EMBEDDING_DIM
        self._instruction = settings.EMBEDDING_QUERY_INSTRUCTION

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        try:
            response = self._client.embeddings.create(model=self._model, input=texts)
        except openai.BadRequestError as exc:
            raise _classify_too_large(exc) from exc
        except openai.APIStatusError as exc:
            if exc.status_code == 413:
                raise EmbeddingPayloadTooLargeError(
                    f"Embedding service rejected payload as too large (413): {exc}"
                ) from exc
            raise  # 429, 5xx etc. — propagate as the typed SDK exception

        raw = [list(item.embedding) for item in response.data]
        return _normalize_response(raw, len(texts), self._dim)

    def embed_query(self, text: str) -> list[float]:
        prefixed = f"{self._instruction}{text}" if self._instruction else text
        vectors = self.embed_documents([prefixed])
        if not vectors:
            raise EmbeddingClientError("Embedding service returned no vectors for query")
        return vectors[0]

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> EmbeddingClient:
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest radis/pgsearch/tests/test_embedding_client.py -v`
Expected: all tests pass. If `test_429_propagates_as_typed_rate_limit_error` fails because the SDK's default retry behavior interferes, double-check that the new client passes `max_retries=0` to `openai.OpenAI(...)`.

- [ ] **Step 5: Commit**

```bash
git add radis/pgsearch/utils/embedding_client.py radis/pgsearch/tests/test_embedding_client.py
git commit -m "refactor(pgsearch): EmbeddingClient on openai SDK; structured too-large detection"
```

---

### Task 3: Widen stamina retry predicate to include typed openai transient errors

**Files:**
- Modify: `radis/pgsearch/tasks.py:25-34` (`_is_retryable_embedding_error`)
- Modify: `radis/pgsearch/tests/test_embed_reports_task.py` (add coverage for the new predicate matches)

**Interfaces:**
- Consumes: `openai.APIConnectionError`, `openai.APITimeoutError`, `openai.InternalServerError`, `openai.RateLimitError` from the `openai` package (already imported by other modules; add a top-of-file import here).
- Produces: `_is_retryable_embedding_error(exc) -> bool` returns `True` for `EmbeddingClientError` (existing) and the three transient SDK errors above; explicitly returns `False` for `EmbeddingPayloadTooLargeError` (existing) and `openai.RateLimitError` (new — 429 must reach the future rate-limit gate, not be silently retried by stamina).

- [ ] **Step 1: Write the failing tests**

Append to `radis/pgsearch/tests/test_embed_reports_task.py`:

```python
def test_predicate_retries_openai_connection_error():
    import openai

    from radis.pgsearch.tasks import _is_retryable_embedding_error

    exc = openai.APIConnectionError(request=None)  # type: ignore[arg-type]
    assert _is_retryable_embedding_error(exc) is True


def test_predicate_retries_openai_internal_server_error():
    import openai

    from radis.pgsearch.tasks import _is_retryable_embedding_error

    # InternalServerError is an APIStatusError subclass; construct via the
    # SDK's __init__ which only requires message + response + body in modern
    # versions. Use a minimal httpx.Response to satisfy the signature.
    import httpx
    response = httpx.Response(503, request=httpx.Request("POST", "http://x"))
    exc = openai.InternalServerError(message="boom", response=response, body=None)
    assert _is_retryable_embedding_error(exc) is True


def test_predicate_does_not_retry_openai_rate_limit_error():
    """429 must reach the future rate-limit gate, not be silently retried."""
    import httpx
    import openai

    from radis.pgsearch.tasks import _is_retryable_embedding_error

    response = httpx.Response(429, request=httpx.Request("POST", "http://x"))
    exc = openai.RateLimitError(message="slow", response=response, body=None)
    assert _is_retryable_embedding_error(exc) is False


def test_predicate_does_not_retry_payload_too_large_error():
    """Unchanged behavior — verify the existing exclusion still holds after the widening."""
    from radis.pgsearch.utils.embedding_client import EmbeddingPayloadTooLargeError

    from radis.pgsearch.tasks import _is_retryable_embedding_error

    assert _is_retryable_embedding_error(EmbeddingPayloadTooLargeError("x")) is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest radis/pgsearch/tests/test_embed_reports_task.py -v -k predicate`
Expected: `test_predicate_retries_openai_connection_error` and `test_predicate_retries_openai_internal_server_error` fail (predicate returns False); `test_predicate_does_not_retry_openai_rate_limit_error` passes by accident (RateLimitError isn't EmbeddingClientError); the existing `_is_retryable_embedding_error` returns True for any `EmbeddingClientError`.

- [ ] **Step 3: Widen the predicate**

Edit `radis/pgsearch/tasks.py`. Replace lines 1-7 (imports) and 25-34 (the predicate):

```python
import logging
import time

import openai
import stamina
import stamina.instrumentation
from django.conf import settings
from procrastinate.contrib.django import app
from procrastinate.types import JSONValue
```

```python
# Transient classes we retry. 429 (RateLimitError) is deliberately NOT here —
# it must reach the rate-limit gate so the worker backs off in coordination
# rather than each thread re-discovering the limit on its own retries.
_RETRYABLE_OPENAI_ERRORS: tuple[type[BaseException], ...] = (
    openai.APIConnectionError,
    openai.APITimeoutError,
    openai.InternalServerError,
)


def _is_retryable_embedding_error(exc: Exception) -> bool:
    """stamina retry predicate. Retry transient failures from either the old
    EmbeddingClientError surface or the typed openai SDK surface. Exclude:
    - EmbeddingPayloadTooLargeError (deterministic; bisect handles it)
    - openai.RateLimitError (gate handles it; retrying would silence the 429)."""
    if isinstance(exc, EmbeddingPayloadTooLargeError):
        return False
    if isinstance(exc, openai.RateLimitError):
        return False
    if isinstance(exc, EmbeddingClientError):
        return True
    return isinstance(exc, _RETRYABLE_OPENAI_ERRORS)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest radis/pgsearch/tests/test_embed_reports_task.py -v -k predicate`
Expected: all four predicate tests pass.

- [ ] **Step 5: Run the full test file to check nothing regressed**

Run: `uv run pytest radis/pgsearch/tests/test_embed_reports_task.py -v`
Expected: all tests pass. The existing tests inject `EmbeddingClientError` instances via mocks and expect retries; the widened predicate is a strict superset for that class.

- [ ] **Step 6: Commit**

```bash
git add radis/pgsearch/tasks.py radis/pgsearch/tests/test_embed_reports_task.py
git commit -m "feat(pgsearch): retry typed openai transient errors; exclude RateLimitError"
```

---

### Task 4: Widen read-path exception clauses to catch typed openai errors

**Files:**
- Modify: `radis/pgsearch/providers.py:24` (import), `:111` and `:224` (except clauses)
- Modify: `radis/pgsearch/tests/test_provider_hybrid.py` (add coverage for the new fallback trigger)

**Interfaces:**
- Consumes: `openai.OpenAIError` base class.
- Produces: `search()` and `retrieve()` fall back to FTS-only when `embed_query` raises any `openai.OpenAIError` subclass (RateLimitError, InternalServerError, APIConnectionError, BadRequestError, ...) — same fallback as today's `EmbeddingClientError` path.

- [ ] **Step 1: Write the failing test**

Append to `radis/pgsearch/tests/test_provider_hybrid.py` (it already imports `patch`, `pytest`, `search`, `Search`, `SearchFilters`, `EmbeddingClientError`, and has the `_make_search` and `reports_with_embeddings` fixtures the snippet uses). Mirror the shape of the existing `test_embedding_failure_falls_back_to_fts` (line 104) which is the EmbeddingClientError parallel:

```python
def test_openai_rate_limit_error_falls_back_to_fts(group, reports_with_embeddings):
    """A 429 from the embedding service on the read path must trigger the FTS
    fallback, not bubble to the search view. This is the typed-openai parallel
    of test_embedding_failure_falls_back_to_fts."""
    import httpx
    import openai

    r0, _, r2 = reports_with_embeddings
    response = httpx.Response(429, request=httpx.Request("POST", "http://x"))
    rate_limit_exc = openai.RateLimitError(
        message="slow down", response=response, body=None
    )
    with patch("radis.pgsearch.providers.EmbeddingClient") as MockClient:
        MockClient.return_value.__enter__.return_value = MockClient.return_value
        MockClient.return_value.__exit__.return_value = None
        MockClient.return_value.embed_query.side_effect = rate_limit_exc
        result = search(_make_search("pneumothorax", group.pk))

    ids = [d.document_id for d in result.documents]
    # FTS-only matches come back; no exception escaped.
    assert set(ids) == {r0.document_id, r2.document_id}


def test_openai_rate_limit_error_in_retrieve_falls_back_to_fts(group, reports_with_embeddings):
    """Same parallel for retrieve()."""
    import httpx
    import openai

    r0, _, r2 = reports_with_embeddings
    response = httpx.Response(429, request=httpx.Request("POST", "http://x"))
    rate_limit_exc = openai.RateLimitError(
        message="slow down", response=response, body=None
    )
    with patch("radis.pgsearch.providers.EmbeddingClient") as MockClient:
        MockClient.return_value.__enter__.return_value = MockClient.return_value
        MockClient.return_value.__exit__.return_value = None
        MockClient.return_value.embed_query.side_effect = rate_limit_exc
        result = retrieve(_make_search("pneumothorax", group.pk))

    # No exception escaped; FTS-only retrieve returned something.
    assert result is not None
```

(`retrieve` is already imported at top of file alongside `search`.)

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest radis/pgsearch/tests/test_provider_hybrid.py -v -k rate_limit`
Expected: `openai.RateLimitError` escapes `PgSearchProvider().search()` because the `except` clause only catches `EmbeddingClientError`.

- [ ] **Step 3: Widen the except clauses in providers.py**

Edit `radis/pgsearch/providers.py`. At line 24 (or alongside the existing embedding-client import), add:

```python
import openai
```

At line 111 (inside `search()`):

```python
        except (EmbeddingClientError, openai.OpenAIError) as e:
            logger.warning("Hybrid search falling back to FTS-only: %s", e)
            query_vec = None
```

At line 224 (inside `retrieve()`):

```python
        except (EmbeddingClientError, openai.OpenAIError) as e:
            logger.warning("Hybrid retrieve falling back to FTS-only: %s", e)
            query_vec = None
```

(Use the exact existing log message text from each clause; the snippet above shows the shape.)

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run pytest radis/pgsearch/tests/test_provider_hybrid.py -v -k rate_limit`
Expected: PASS.

- [ ] **Step 5: Run the full hybrid-provider test file**

Run: `uv run pytest radis/pgsearch/tests/test_provider_hybrid.py -v`
Expected: all tests pass.

- [ ] **Step 6: Commit**

```bash
git add radis/pgsearch/providers.py radis/pgsearch/tests/test_provider_hybrid.py
git commit -m "feat(pgsearch): read-path falls back to FTS on typed openai errors"
```

---

### Task 5: Remove legacy settings and update example.env

**Files:**
- Modify: `radis/settings/base.py:340-342` (remove `EMBEDDING_BACKEND`, `EMBEDDING_PROVIDER_PATH`)
- Modify: `example.env:137-169` (rewrite the embedding block)

**Interfaces:**
- Consumes: nothing.
- Produces: `settings.EMBEDDING_BACKEND` and `settings.EMBEDDING_PROVIDER_PATH` no longer exist. Any startup that still has the env vars set fails the system check from Task 1.

- [ ] **Step 1: Remove the settings lines**

Edit `radis/settings/base.py`. Delete these two lines (currently around line 340-342):

```python
EMBEDDING_BACKEND = env.str("EMBEDDING_BACKEND", default="openai")
EMBEDDING_PROVIDER_PATH = env.str("EMBEDDING_PROVIDER_PATH", default="")
```

Leave the surrounding lines (`EMBEDDING_PROVIDER_URL`, `EMBEDDING_PROVIDER_API_KEY`, `EMBEDDING_MODEL_NAME`, `EMBEDDING_DIM`) intact.

- [ ] **Step 2: Rewrite the example.env block**

Edit `example.env`. Replace the current embedding-service block (lines 137-169 or thereabouts — the comment block starts with `# Embedding service configuration (used by radis.pgsearch for hybrid search).`) with:

```env
# Embedding service (OpenAI-compatible /v1/embeddings).
# Independent of the LLM service above. Any provider that exposes the
# OpenAI embeddings endpoint works: OpenAI, Azure OpenAI, vLLM, TEI, an
# LLM gateway, or Ollama via its /v1 compatibility layer.
#
# Examples (note the /v1 suffix on the base URL):
#   EMBEDDING_PROVIDER_URL=https://api.openai.com/v1
#   EMBEDDING_PROVIDER_URL=http://host.docker.internal:11434/v1   # Ollama (dev)
EMBEDDING_PROVIDER_URL=

# Bearer token. Send as "Authorization: Bearer <key>". For self-hosted
# endpoints that ignore auth (Ollama, dev vLLM) leave empty.
EMBEDDING_PROVIDER_API_KEY=

# The model name to request from the embedding service.
EMBEDDING_MODEL_NAME=Qwen/Qwen3-Embedding-4B

# Vector dimension. Schema-coupled: changing this after deploy requires dropping
# the embedding column, re-migrating, and running `./manage.py embed_pending`.
EMBEDDING_DIM=1024
```

- [ ] **Step 3: Run the full test suite to confirm nothing reads the removed settings**

Run: `uv run cli test -- radis/pgsearch/`
Expected: all pgsearch tests pass. If any test still calls `override_settings(EMBEDDING_BACKEND=..., EMBEDDING_PROVIDER_PATH=...)`, it'll fail at `override_settings` resolution; remove those kwargs.

- [ ] **Step 4: Commit**

```bash
git add radis/settings/base.py example.env
git commit -m "refactor(pgsearch): drop EMBEDDING_BACKEND/PATH settings; rewrite env recipe"
```

---

### Task 6: Final verification

**Files:** none (verification only).

- [ ] **Step 1: Full test suite**

Run: `uv run cli test`
Expected: all tests pass. No `httpx.HTTPError`-typed assertions, no `OllamaBackend` / `OpenAIBackend` / `BACKENDS` imports left in the tree.

- [ ] **Step 2: Lint clean**

Run: `uv run cli lint`
Expected: zero errors. (Ruff E/F/I/DJ rules; line length 100.)

- [ ] **Step 3: Pyright clean**

Run: `uv run pyright radis/pgsearch/`
Expected: zero errors. Common gotcha: `openai.OpenAI.embeddings.create` returns `CreateEmbeddingResponse`; `response.data` is a list of `Embedding`; `Embedding.embedding` is `list[float]` (or `bytes` if `encoding_format="base64"` — we don't pass that, so the type narrows to `list[float]`).

- [ ] **Step 4: Grep for legacy symbols to confirm clean removal**

Run: `grep -rn "EMBEDDING_BACKEND\|EMBEDDING_PROVIDER_PATH\|OllamaBackend\|OpenAIBackend\|BACKENDS\|_TOO_LARGE_MARKERS" radis/ example.env`
Expected: no hits outside `radis/pgsearch/apps.py` (the system check function body, which references the env var names in the error message).

- [ ] **Step 5: Smoke-test against a running embedding service (optional, manual)**

If a dev Ollama or staging vLLM is available, point `.env` at it (URL ending in `/v1`) and run:

```bash
uv run cli compose-up
# wait for services
uv run cli shell -- -c "
from radis.pgsearch.utils.embedding_client import EmbeddingClient
with EmbeddingClient() as c:
    v = c.embed_query('test')
    print(f'dim={len(v)}, head={v[:4]}')"
```

Expected: prints a vector of length `EMBEDDING_DIM`. If you point at a non-`/v1` URL, expect `openai.NotFoundError` (404) — that's the migration's intended user-facing failure mode for a stale URL.

- [ ] **Step 6: Update CLAUDE.md (optional)**

If `CLAUDE.md` mentions `EMBEDDING_BACKEND` anywhere (it doesn't currently — verified at plan time), remove the reference. Skip otherwise.

- [ ] **Step 7: Final commit only if any verification step revealed a fix**

If a tiny fix was needed (e.g. a stray import, a missed `override_settings` kwarg), commit it as its own change. Do not amend earlier commits.

---

## Out of scope (separate work)

- Rate-limit gate. A follow-up spec (sketched in conversation 2026-06-30) lifts `RateLimitGate` from `radis/chats/utils/rate_limit.py` to a project-level home and wraps `EmbeddingClient`. This plan deliberately stops once the typed exceptions reach callers — the gate is one additional class away.
- Proactive `EMBEDDING_MAX_RPM` / `EMBEDDING_MAX_TPM` (pyrate-limiter). Separate spec, separate plan.
- Re-embedding stored vectors. The wire format is unchanged; existing `ReportSearchIndex.embedding` rows remain valid.
- ADRF / `AsyncEmbeddingClient`. Not adopted (see project memory).
