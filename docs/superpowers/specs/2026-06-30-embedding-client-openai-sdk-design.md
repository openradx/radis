# Embedding client on the OpenAI SDK — design

**Status:** Draft (2026-06-30)
**Author:** RADIS team (Samuel Kwong)
**Scope:** `radis.pgsearch.utils.embedding_client` and its callers (`radis/pgsearch/tasks.py`, `radis/pgsearch/providers.py`, `radis/pgsearch/admin.py`, `radis/pgsearch/management/commands/embed_pending.py`). Settings: `radis/settings/base.py`, `example.env`.
**Related:** `2026-05-28-hybrid-search.md` (originating spec), `2026-06-24-llm-rate-limit-handling-design.md` (PR #231; enables the follow-up gate this spec unblocks).

## Motivation

Today's `EmbeddingClient` ships its own dual-backend abstraction (`OpenAIBackend` / `OllamaBackend`) layered over a raw `httpx.Client`. Three costs from that choice are now visible:

1. **Substring-matched 4xx detection is brittle.** `_is_payload_too_large` greps the response body for a list of free-text markers (`"context length"`, `"max tokens"`, `"too long"`, …) to decide whether to bisect the batch. The list false-positives on unrelated 4xx (e.g. a request whose body mentions `max_tokens`), drifts silently on provider version bumps, and is untestable against real upstream changes.
2. **The pluggable backend has no remaining justification.** Ollama exposes `/v1/embeddings` as an OpenAI-compatible endpoint (`{model, input: string | array}` → `{data: [{embedding, index}]}`). The `OllamaBackend` native path (`/api/embed`) is no longer the only way to talk to a local Ollama. Maintaining two payload/response codecs costs surface area for zero deployment flexibility we don't get from `EMBEDDING_PROVIDER_URL` alone.
3. **Rate-limit handling has no typed signal.** PR #231 (`feature/auto-labeling_muhammad`) ships a `RateLimitGate` in `radis/chats/utils/rate_limit.py` that keys off `openai.RateLimitError` and `_parse_retry_after(exc)` (reads `Retry-After` / `retry-after-ms` / HTTP-date from `exc.response.headers`). Reusing that gate for embeddings is the natural follow-up — but only if the embedding HTTP call raises the same typed exceptions. With raw httpx, the gate would need a parallel detector for 429 + a parallel `Retry-After` parser.

The LLM side (`radis/chats/utils/chat_client.py`) already runs against any OpenAI-compatible endpoint through the `openai` SDK with no per-provider switch. This spec aligns the embedding client with that approach.

## Goals

- One code path for every embedding provider (OpenAI, Azure OpenAI, vLLM, an LLM gateway, Ollama), via the `openai` SDK pointed at `EMBEDDING_PROVIDER_URL`.
- Structured payload-too-large detection via the provider's `error.code`, not substring matching the message.
- Typed exceptions on 429 (`openai.RateLimitError`) and 4xx (`openai.BadRequestError`) so a future rate-limit gate can reuse PR #231's primitives unchanged.
- Preserve every existing semantic of `EmbeddingClient`: Matryoshka truncation to `EMBEDDING_DIM`, unconditional L2 normalization, batched `embed_documents`, single-shot `embed_query` with `EMBEDDING_QUERY_INSTRUCTION` prefix, context-manager close.
- Sync-only client. Both call sites — `embed_reports_task` on the worker and `embed_query` on the read path — are synchronous; ADRF is not in scope. No `AsyncEmbeddingClient`.
- Migration is a documented one-time `.env` change for operators using the Ollama backend; no migration of stored vectors.

## Non-goals

- **Rate-limit handling.** Out of scope here; this spec only makes it implementable as a follow-up. The follow-up will lift `RateLimitGate` from `radis/chats/utils/rate_limit.py` to a project-level home and wrap `EmbeddingClient.embed_documents` / `embed_query` the same way `ThrottledChatClient` wraps `ChatClient.extract_data`.
- **Proactive client-side RPM / TPM caps.** Separate spec (`EMBEDDING_MAX_RPM` / `EMBEDDING_MAX_TPM` via `pyrate-limiter`) sketched in conversation 2026-06-30; deferred.
- **Re-embedding stored vectors.** The on-the-wire format and the parsed vector shape are unchanged. Existing `ReportSearchIndex.embedding` rows stay valid.
- **Behaviour change on the read path.** `providers.search` / `providers.retrieve` keep their current "any embedding error → fall back to FTS-only" policy. The exception types they catch widen from `EmbeddingClientError` to `(EmbeddingClientError, openai.OpenAIError)` (or a single shared base — see §"Errors").
- **Changes to `ChatClient`.** Out of scope; LLM client already on the SDK.

## Decisions

| Decision | Choice | Rationale |
|---|---|---|
| HTTP layer | `openai.OpenAI` (sync only) | Matches `ChatClient`; gives typed `RateLimitError` / `BadRequestError`; honors `Retry-After ≤ 60s` for free. Async variant deliberately omitted: ADRF is not adopted and both call sites are sync. |
| Backend abstraction | Removed | Ollama supports OpenAI-compatible `/v1/embeddings`; the Protocol + dict has no remaining payer. |
| Payload-too-large detection | `BadRequestError.body["error"]["code"] in {"context_length_exceeded", "string_above_max_length"}` | Structured code, stable across versions, testable. No substring matching. |
| Detection on non-conforming providers | Treat as "not too large" → propagate as normal `EmbeddingClientError`; do not bisect | Bisecting on the wrong error is worse than not bisecting on a real one (which still surfaces via Procrastinate retry → eventual operator attention). |
| HTTP 413 | Still treated as too-large (no body inspection needed) | Unambiguous; cheap to keep. |
| `EMBEDDING_BACKEND` env | Removed | Single backend; setting it is now meaningless. |
| `EMBEDDING_PROVIDER_PATH` env | Removed | SDK constructs paths from `base_url`; per-path override no longer applies. |
| `EMBEDDING_PROVIDER_URL` env | Kept; passed to SDK as `base_url`; documented to end in `/v1` | Same convention the SDK uses everywhere; matches `EXTERNAL_LLM_PROVIDER_URL` semantics. |
| `EMBEDDING_PROVIDER_API_KEY` env | Kept; passed to SDK as `api_key` | Unchanged semantics. |
| Legacy-var detection | Django system check (`pgsearch.E002`) raises at startup if `EMBEDDING_BACKEND` or `EMBEDDING_PROVIDER_PATH` is set | Loud, one-shot migration signal. No silent ignore. |
| Matryoshka truncation + L2 normalize | Unchanged; applied to `CreateEmbeddingResponse.data[*].embedding` | Same `_normalize_response` helper, fed the SDK's parsed vectors. |
| Client construction | One thin class around `openai.OpenAI` | Mirrors sync `ChatClient`. No async variant. |
| `SDK max_retries` | `0` | Surfaces 429 immediately to caller so a future gate can arm at once (same rationale as `ThrottledChatClient`). 5xx/connection resilience handled separately if needed (see §"Errors"). |
| `SDK timeout` | `settings.EMBEDDING_REQUEST_TIMEOUT` (already exists, default 30s) | Preserves current behaviour; explicit override prevents the SDK's 600s default. |
| Test transport | `respx` mounted on the SDK's underlying `httpx.Client` via `openai.OpenAI(http_client=httpx.Client(transport=respx_mock))` | The SDK accepts a custom `http_client`. Same testability story as today's `_build_http_client()` indirection. |

## Architecture

### Module shape

`radis/pgsearch/utils/embedding_client.py` collapses to ~120 lines (from ~210). What goes:

- `EmbeddingBackend` Protocol, `OpenAIBackend`, `OllamaBackend`, `BACKENDS` dict.
- `_TOO_LARGE_MARKERS` tuple and `_is_payload_too_large(response: httpx.Response)`.
- `_build_http_client()` and direct `httpx.Client.post` call.
- `_resolve_config` path/URL assembly.

What stays:

- `EmbeddingClientError`, `EmbeddingPayloadTooLargeError` (semantics unchanged; raised from new sites).
- `_l2_normalize`, `_normalize_response` (Matryoshka truncation + dim validation).
- `EmbeddingClient.embed_documents`, `embed_query`, `close`, `__enter__`, `__exit__`.

What's added:

- `_TOO_LARGE_ERROR_CODES = frozenset({"context_length_exceeded", "string_above_max_length"})`.
- `_classify_too_large(exc: openai.BadRequestError) -> EmbeddingClientError` helper (returns the right *instance* — `EmbeddingPayloadTooLargeError` if the structured code matches, base `EmbeddingClientError` otherwise — so callers `raise _classify_too_large(exc) from exc`).

What's *not* added: an `AsyncEmbeddingClient`. The original hybrid-search spec (`2026-05-28-hybrid-search.md` §5) mentioned one for the ADRF read path, but ADRF is no longer being adopted and both call sites (`embed_reports_task` on the worker, `embed_query` from `providers.search` / `providers.retrieve`) are synchronous. If async is needed later, it's a non-breaking add: a second class wrapping `openai.AsyncOpenAI` with the same surface.

### Client construction

```python
from openai import OpenAI, AsyncOpenAI
from django.conf import settings

def _client_kwargs() -> dict[str, object]:
    base_url = settings.EMBEDDING_PROVIDER_URL
    if not base_url:
        raise EmbeddingClientError("EMBEDDING_PROVIDER_URL is not configured")
    return {
        "base_url": base_url,
        "api_key": settings.EMBEDDING_PROVIDER_API_KEY or "unused",  # SDK requires non-empty
        "max_retries": 0,
        "timeout": settings.EMBEDDING_REQUEST_TIMEOUT,
    }

class EmbeddingClient:
    def __init__(self) -> None:
        self._client = OpenAI(**_client_kwargs())
        self._model = settings.EMBEDDING_MODEL_NAME
        self._dim = settings.EMBEDDING_DIM
        self._instruction = settings.EMBEDDING_QUERY_INSTRUCTION
```

The `api_key="unused"` fallback covers self-hosted endpoints that ignore auth (Ollama, dev vLLM). The SDK rejects an empty string at construction time; sending a placeholder header that the upstream ignores is the documented workaround.

### `embed_documents`

```python
def embed_documents(self, texts: list[str]) -> list[list[float]]:
    try:
        response = self._client.embeddings.create(model=self._model, input=texts)
    except openai.BadRequestError as e:
        raise _classify_too_large(e) from e
    except openai.OpenAIError as e:
        raise EmbeddingClientError(f"Embedding service error: {e}") from e
    raw = [item.embedding for item in response.data]
    return _normalize_response(raw, len(texts), self._dim)
```

### Too-large classification

```python
_TOO_LARGE_ERROR_CODES = frozenset({"context_length_exceeded", "string_above_max_length"})

def _classify_too_large(exc: openai.BadRequestError) -> EmbeddingClientError:
    # 413 surfaces as APIStatusError(status_code=413) — handled in caller.
    body = getattr(exc, "body", None) or {}
    code = (body.get("error") or {}).get("code") if isinstance(body, dict) else None
    snippet = str(exc)[:200]
    if code in _TOO_LARGE_ERROR_CODES:
        return EmbeddingPayloadTooLargeError(
            f"Embedding service rejected payload as too large (code={code}): {snippet}"
        )
    return EmbeddingClientError(f"Embedding service returned 400: {snippet}")
```

HTTP 413 surfaces as `openai.APIStatusError` (not `BadRequestError`). Catch it explicitly:

```python
except openai.APIStatusError as e:
    if e.status_code == 413:
        raise EmbeddingPayloadTooLargeError(
            f"Embedding service rejected payload as too large (413): {e}"
        ) from e
    raise EmbeddingClientError(f"Embedding service returned {e.status_code}: {e}") from e
```

### Caller impact

| File | Change |
|---|---|
| `radis/pgsearch/tasks.py` | `_is_retryable_embedding_error` (stamina predicate at line ~19) widens its retry set: keep `EmbeddingClientError`; **add `openai.APIConnectionError` and `openai.InternalServerError`**; **do not retry `openai.RateLimitError`** (the future gate handles it). Drop the explicit `EmbeddingPayloadTooLargeError` skip — it's still a non-retryable subclass of `EmbeddingClientError`, so the existing `not isinstance(..., EmbeddingPayloadTooLargeError)` clause keeps working. |
| `radis/pgsearch/providers.py` | `search()` / `retrieve()` `except` clauses widen from `EmbeddingClientError` to `(EmbeddingClientError, openai.OpenAIError)`. Fallback-to-FTS semantics unchanged. |
| `radis/pgsearch/admin.py` | No change (does not call the client directly). |
| `embed_pending` cmd | No change (defers tasks; does not call the client directly). |
| Tests using `MockTransport` | Migrate to passing an `httpx.Client(transport=respx_mock)` (or equivalent) as the `openai.OpenAI(http_client=...)` constructor arg. See §"Tests". |

### Settings changes

In `radis/settings/base.py`:

```python
# REMOVED:
# EMBEDDING_BACKEND = env.str("EMBEDDING_BACKEND", default="openai")
# EMBEDDING_PROVIDER_PATH = env.str("EMBEDDING_PROVIDER_PATH", default="")

# KEPT (unchanged):
EMBEDDING_PROVIDER_URL = env.str("EMBEDDING_PROVIDER_URL", default="")
EMBEDDING_PROVIDER_API_KEY = env.str("EMBEDDING_PROVIDER_API_KEY", default="")
EMBEDDING_MODEL_NAME = env.str("EMBEDDING_MODEL_NAME", default="Qwen/Qwen3-Embedding-4B")
EMBEDDING_DIM = env.int("EMBEDDING_DIM", default=1024)
EMBEDDING_REQUEST_TIMEOUT = 30
EMBEDDING_QUERY_INSTRUCTION = env.str("EMBEDDING_QUERY_INSTRUCTION", default="…")
# (batch / subjob / priority constants unchanged)
```

In `example.env`, the Embedding service block becomes a single OpenAI-compatible recipe with a documented Ollama variant:

```env
# Embedding service (OpenAI-compatible /v1/embeddings).
# Production examples:
#   EMBEDDING_PROVIDER_URL=https://api.openai.com/v1
#   EMBEDDING_PROVIDER_URL=https://<project>.openai.azure.com/openai/deployments/<dep>
# Dev (Ollama with OpenAI compatibility layer — note the /v1 suffix):
#   EMBEDDING_PROVIDER_URL=http://host.docker.internal:11434/v1
EMBEDDING_PROVIDER_URL=
EMBEDDING_PROVIDER_API_KEY=
EMBEDDING_MODEL_NAME=Qwen/Qwen3-Embedding-4B
EMBEDDING_DIM=1024
```

### Legacy-var system check

`radis/pgsearch/apps.py` registers a Django system check:

```python
@register()
def check_legacy_embedding_vars(app_configs, **kwargs):
    errors = []
    if os.environ.get("EMBEDDING_BACKEND"):
        errors.append(Error(
            "EMBEDDING_BACKEND is no longer supported; remove it from .env. "
            "All embedding providers now use the OpenAI-compatible /v1/embeddings endpoint.",
            id="pgsearch.E002",
        ))
    if os.environ.get("EMBEDDING_PROVIDER_PATH"):
        errors.append(Error(
            "EMBEDDING_PROVIDER_PATH is no longer supported; append the path to "
            "EMBEDDING_PROVIDER_URL instead (e.g. http://host:11434/v1).",
            id="pgsearch.E003",
        ))
    return errors
```

System checks run on every `manage.py` invocation, so the first `migrate` / `runserver` after deploy surfaces this. No silent ignore, no half-broken state.

## Errors

The post-change exception hierarchy:

- `EmbeddingClientError(Exception)` — base; all errors callers care about.
- `EmbeddingPayloadTooLargeError(EmbeddingClientError)` — bisect signal.
- `openai.OpenAIError` and subclasses (`RateLimitError`, `APIConnectionError`, `InternalServerError`, `BadRequestError`, `APIStatusError`, …) — surfaced as themselves when callers want to discriminate (rate-limit gate, stamina retry predicate). For callers that don't care about the discrimination, `(EmbeddingClientError, openai.OpenAIError)` is the catch-all in the read-path fallback.

Decision *not* to wrap `openai.OpenAIError` in `EmbeddingClientError`: the typed exceptions are precisely what makes the future gate cheap to add. Wrapping would erase them on the way out and force re-detection downstream.

| Failure mode | Raised as | Treated by caller |
|---|---|---|
| 200 with malformed body | (cannot happen — SDK parses to typed model; mismatch raises `openai.OpenAIError`) | Read path: fall back to FTS. Write path: stamina retry. |
| 400 with `code=context_length_exceeded` | `EmbeddingPayloadTooLargeError` | Write path: `_embed_with_bisect`. Read path: fall back to FTS. |
| 400 with any other code | `EmbeddingClientError` | Read path: fall back to FTS. Write path: NOT retried (deterministic). |
| 413 | `EmbeddingPayloadTooLargeError` | Same as above. |
| 429 | `openai.RateLimitError` | Today: stamina does **not** retry (predicate excludes it); Procrastinate retries the whole task. Future: gate intercepts, honors `Retry-After`, coordinates worker-wide backoff. |
| 5xx | `openai.InternalServerError` | Stamina retries (3 attempts). |
| Connection / timeout | `openai.APIConnectionError` / `openai.APITimeoutError` | Stamina retries. |
| Misconfigured base_url at startup | `EmbeddingClientError` ("EMBEDDING_PROVIDER_URL is not configured") | Read path: fall back to FTS. Write path: task fails; Procrastinate retries; operator fixes config. |

## Tests

`radis/pgsearch/tests/test_embedding_client.py` — keep coverage, migrate fixture style:

| Existing test | After |
|---|---|
| `test_openai_backend_payload_round_trip` | Drop. The SDK round-trip is OpenAI's contract. |
| `test_ollama_backend_payload_round_trip` | Drop. Ollama supported via the OpenAI path. |
| `test_provider_path_override` | Drop. Setting removed. |
| `test_query_instruction_prefix` | Keep. Verify `embed_query` POSTs `instruction + text`. |
| `test_l2_normalization` | Keep. Pure helper test, unchanged. |
| `test_dim_validation_short_vector` | Keep. |
| `test_matryoshka_truncation` | Keep. |
| `test_too_large_via_413` | Keep. Inject 413 response via `respx`; assert `EmbeddingPayloadTooLargeError`. |
| `test_too_large_via_400_with_marker` | Replace with `test_too_large_via_400_with_code` (code = `"context_length_exceeded"`). |
| `test_400_without_known_marker_is_not_too_large` | Replace with `test_400_without_known_code_is_not_too_large` (e.g. `code="invalid_request"`). |
| (new) `test_400_with_no_error_object_is_not_too_large` | Edge case: provider returns 400 with non-OpenAI body shape. |
| (new) `test_rate_limit_error_propagates_as_typed` | Inject 429; assert `openai.RateLimitError` reaches the caller (does **not** get wrapped). |
| (new) `test_connection_error_propagates_as_typed` | Inject connection failure; assert `openai.APIConnectionError` reaches the caller. |

Test transport pattern:

```python
import httpx, respx
from openai import OpenAI

def _patched_client(transport: httpx.MockTransport) -> EmbeddingClient:
    ec = EmbeddingClient.__new__(EmbeddingClient)
    ec._client = OpenAI(base_url="http://test/v1", api_key="t", http_client=httpx.Client(transport=transport))
    ec._model = "test-model"
    ec._dim = 4
    ec._instruction = "Q: "
    return ec
```

`radis/pgsearch/tests/test_tasks.py` — one update: where existing tests assert `httpx.HTTPError` propagation, change to inject `openai.InternalServerError` (or whichever typed exception the test is exercising) and assert the same stamina-retry / Procrastinate-retry behaviour.

`radis/pgsearch/tests/test_legacy_env_check.py` — new, two cases: setting `EMBEDDING_BACKEND` or `EMBEDDING_PROVIDER_PATH` in env produces the corresponding system check error.

## Migration / rollout

**Operator action required** (single .env edit):

1. If using OpenAI / Azure / vLLM / a gateway and `EMBEDDING_BACKEND=openai`: remove the line. URL likely already ends in `/v1`; no other change.
2. If using Ollama with `EMBEDDING_BACKEND=ollama` and `EMBEDDING_PROVIDER_URL=http://host:11434`: change to `EMBEDDING_PROVIDER_URL=http://host:11434/v1` and remove `EMBEDDING_BACKEND`.
3. If `EMBEDDING_PROVIDER_PATH` is set: append it to `EMBEDDING_PROVIDER_URL` (most likely you can just delete the override; the OpenAI-compat path is always `/embeddings` relative to `/v1`).

The system check above produces a clear error on startup if step 1–3 is missed.

**No code-level migration:**

- No DB migration. Stored embedding vectors are unchanged on the wire.
- No re-embedding. Output of `OpenAI().embeddings.create()` for the same model on the same input is byte-identical to the raw `httpx.post` we do today.
- Worker images need `openai` in `pyproject.toml` — already present (used by `ChatClient`). No new dependency.

## Future work this unblocks

A follow-up spec can lift `RateLimitGate` + `run_through_gate` + `_parse_retry_after` from `radis/chats/utils/rate_limit.py` to a project-level home (e.g. `radis/core/utils/rate_limit.py`) and wrap `EmbeddingClient`:

```python
class ThrottledEmbeddingClient:
    def __init__(self, client: EmbeddingClient) -> None:
        self._client = client

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return run_through_gate(
            _EMBEDDING_GATE,
            settings.EMBEDDING_RATE_LIMIT_MAX_WAIT_SECONDS,
            lambda: self._client.embed_documents(texts),
        )
```

The labeling path's `ThrottledChatClient` becomes a sibling, both consuming the same gate primitives. No further work in `embedding_client.py` itself.

## Alternatives considered

1. **Keep the dual backend; only fix the marker list.** Smallest diff. Rejected because the rate-limit follow-up is the main downstream consumer of this spec; reaching for `openai.RateLimitError` from raw httpx means re-implementing the SDK's HTTP-error classification, and at that point the SDK earns its place.
2. **Keep raw httpx; add a hand-rolled `RateLimitError` and `_parse_retry_after` for the embedding path.** Two parallel implementations of the same logic across the codebase (PR #231's already in `chats/utils`, ours in `pgsearch/utils`). Cheaper today, more code to maintain forever.
3. **Move to `openai` SDK but keep `EMBEDDING_BACKEND` as a no-op for back-compat.** Soft migration. Rejected — the system check is loud, the .env edit is one line, and silent-ignore on a removed setting is the kind of thing that surfaces as confusion six months later. Cleaner to fail at startup.
4. **Detect too-large by tokenizing inputs client-side before the call.** Would prevent the bad request entirely, but requires shipping the model's tokenizer (Qwen3-Embedding ≈ 50MB) into the worker image and binding the validation to a specific model. Out of proportion for an error that already has a structured upstream signal we can read.
