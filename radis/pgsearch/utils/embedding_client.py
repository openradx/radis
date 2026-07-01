from __future__ import annotations

import logging
import math

import httpx
import openai
from django.conf import settings

from .rate_limiter import acquire_search_priority_token, call_with_rate_limit

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

# Some OpenAI-compat gateways don't set a descriptive string `error.code` at
# all — confirmed empirically against the real production embedding gateway
# (the internal embedding gateway), which echoes the HTTP status as `code` (e.g. `400`,
# not `"context_length_exceeded"`) but still sets the structured
# `error.param` field to name the offending parameter. A context-length
# rejection there reports `param="input_tokens"` with a message like "This
# model's maximum context length is 16384 tokens...". Checking `param` is
# still a structured-field match, not substring matching on the message.
_TOO_LARGE_ERROR_PARAMS = frozenset({"input_tokens"})


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
            f"Embedding count mismatch: requested {expected_count}, backend returned {len(raw)}"
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
    error, based on the structured error.code / error.param. Non-OpenAI-shaped
    bodies (neither field set) are deliberately treated as NOT too-large —
    bisecting on the wrong error is worse than not bisecting on a real one.

    The SDK promotes the JSON `error.code` / `error.param` fields onto
    `exc.code` / `exc.param` directly, so we read those rather than
    navigating `exc.body["error"][...]`. Checked in order: a canonical
    `code` match first, then the `param="input_tokens"` fallback for
    gateways that don't set a descriptive code (see `_TOO_LARGE_ERROR_PARAMS`)."""
    code: str | None = exc.code
    param: str | None = exc.param
    snippet = str(exc)[:200]
    if code in _TOO_LARGE_ERROR_CODES:
        return EmbeddingPayloadTooLargeError(
            f"Embedding service rejected payload as too large (code={code}): {snippet}"
        )
    if param in _TOO_LARGE_ERROR_PARAMS:
        return EmbeddingPayloadTooLargeError(
            f"Embedding service rejected payload as too large (param={param}): {snippet}"
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
            # encoding_format="float" requests JSON-float vectors. Without this
            # the SDK defaults to base64, which would require a decode step
            # back to floats — extra work and a less debuggable wire format.
            response = self._client.embeddings.create(
                model=self._model, input=texts, encoding_format="float"
            )
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
        vectors = call_with_rate_limit(
            acquire_search_priority_token,
            lambda: self.embed_documents([prefixed]),
        )
        if not vectors:
            raise EmbeddingClientError("Embedding service returned no vectors for query")
        return vectors[0]

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> EmbeddingClient:
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()
