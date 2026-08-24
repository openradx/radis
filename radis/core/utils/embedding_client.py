from __future__ import annotations

import logging
import math

import httpx
import openai
from django.conf import settings

from radis.core.utils.rate_limit import RateLimitGate, run_through_gate

logger = logging.getLogger(__name__)

# Process-global so every embedding caller in this worker/web process shares one backoff
# window. Deliberately separate from the LLM gate in core.utils.llm_client: the embedding
# gateway is a different provider, so a 429 from one must not pause the other. Cross-process
# coordination is unnecessary — each container backs off on the 429s it receives itself.
EMBEDDING_GATE = RateLimitGate(
    base_seconds=settings.EMBEDDINGS_RATE_LIMIT_BACKOFF_BASE_SECONDS,
    backoff_max_seconds=settings.EMBEDDINGS_RATE_LIMIT_BACKOFF_MAX_SECONDS,
    header_ceiling_seconds=settings.EMBEDDINGS_RATE_LIMIT_HEADER_CEILING_SECONDS,
)


# Typed SDK errors that mean the request reached the service but the config is
# wrong (bad key/permission, wrong model or endpoint, malformed request).
# Retrying won't fix them, so callers fail fast and (write path) log a clear
# config error / (read path) fall back to FTS-only.
PERMANENT_EMBEDDING_ERRORS = (
    openai.AuthenticationError,
    openai.PermissionDeniedError,
    openai.NotFoundError,
    openai.BadRequestError,
)


class EmbeddingClientError(Exception):
    """Raised when the embedding service returns a malformed response or when
    configuration is invalid. Typed `openai.OpenAIError` subclasses
    (RateLimitError, BadRequestError, InternalServerError, ...) are NOT wrapped
    in this class — callers that want to discriminate (the transient retry
    layer, the rate-limit gate) match on the SDK types directly."""


def _build_http_client() -> httpx.Client:
    """Indirection so tests can swap in an httpx.MockTransport. The returned
    client is passed to openai.OpenAI(http_client=...); the SDK applies
    EMBEDDINGS_REQUEST_TIMEOUT_SECONDS per request, so no timeout is set here."""
    return httpx.Client()


def _l2_normalize(vec: list[float]) -> list[float]:
    """L2-normalize ``vec`` to a unit vector, rejecting values that can't be.

    A zero vector has undefined cosine distance and is unusable by a
    ``vector_cosine_ops`` HNSW index; non-finite components (NaN/inf) corrupt
    both indexing and query distances. Both indicate malformed provider
    output, so raise ``EmbeddingClientError`` rather than let them silently
    reach pgvector."""
    if not all(math.isfinite(x) for x in vec):
        raise EmbeddingClientError("Embedding contains non-finite values (NaN or inf)")
    norm = math.sqrt(sum(x * x for x in vec))
    if norm == 0.0:
        raise EmbeddingClientError("Embedding is a zero vector; cosine distance is undefined")
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
            # Matryoshka truncation: keep first EMBEDDINGS_DIM components, then renormalize.
            # Qwen3-Embedding is trained to retain quality at truncated dimensions.
            normalized.append(_l2_normalize(list(vec[:target_dim])))
        else:
            # Length already matches; still normalize since we can't assume
            # all providers return unit vectors.
            normalized.append(_l2_normalize(list(vec)))
    return normalized


class EmbeddingClient:
    """Sync embedding client over the openai SDK. Single OpenAI-compatible
    endpoint (set EMBEDDINGS_BASE_URL to end in /v1). Same shape for OpenAI,
    Azure, vLLM, an LLM gateway, or Ollama's /v1 compatibility layer."""

    def __init__(self) -> None:
        spec = settings.EMBEDDINGS_MODEL
        if spec is None:
            raise EmbeddingClientError(
                "EMBEDDINGS_MODEL is not configured; hybrid search is disabled"
            )
        # SDK rejects an empty api_key at construction; "unused" is the documented
        # placeholder for self-hosted endpoints that ignore auth (Ollama, vLLM).
        api_key = settings.EMBEDDINGS_API_KEY or "unused"
        self._http = _build_http_client()
        self._client = openai.OpenAI(
            base_url=settings.EMBEDDINGS_BASE_URL,
            api_key=api_key,
            http_client=self._http,
            max_retries=0,  # 429s are handled by the rate-limit gate, not the SDK
            timeout=settings.EMBEDDINGS_REQUEST_TIMEOUT_SECONDS,
        )
        self._model = spec.model
        # Request parameters configured with the model, e.g. OpenAI's `dimensions`. Copied
        # rather than aliased: spec.params is the live settings.EMBEDDINGS_MODEL.params dict,
        # and every call below hands it to the SDK as extra_body -- nothing mutates it today,
        # but a shallow copy costs one line and rules out an accidental top-level mutation
        # (assignment, pop, clear) corrupting settings for later calls. It does not protect
        # nested values (e.g. a `chat_template_kwargs.*` param), which stay shared; those
        # would need a deep copy, not worth it for the scalar params actually in use today.
        self._extra_body = dict(spec.params)
        self._dim = settings.EMBEDDINGS_DIM
        self._instruction = settings.EMBEDDINGS_QUERY_INSTRUCTION

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """Low-level call to the embedding backend, with no 429 handling of
        its own — `embed_query` (below) and `_embed_chunk_with_retry` in
        `radis/pgsearch/tasks.py` run it through `EMBEDDING_GATE`. HTTP
        errors (400, 429, 5xx, ...) propagate as typed SDK exceptions.

        WARNING: this call passes both `encoding_format="float"` and
        `extra_body=self._extra_body` (the EMBEDDINGS_MODEL spec's params) to the
        SDK. If a spec sets its own `encoding_format` (e.g.
        `qwen3?encoding_format=base64`), `extra_body` wins the SDK's body merge and
        silently overrides the literal above — the backend then returns base64
        strings instead of float lists, which `_normalize_response` does not
        expect and will misbehave on. Do not put `encoding_format` in a model
        spec's params."""
        # encoding_format="float" requests JSON-float vectors. Without this
        # the SDK defaults to base64, which would require a decode step
        # back to floats — extra work and a less debuggable wire format.
        response = self._client.embeddings.create(
            model=self._model,
            input=texts,
            encoding_format="float",
            extra_body=self._extra_body,
        )
        raw = [list(item.embedding) for item in response.data]
        return _normalize_response(raw, len(texts), self._dim)

    def embed_query(self, text: str) -> list[float]:
        """Embed a search query through the gate with the short query budget:
        a user is waiting, so when the gate is closed beyond that budget this
        raises RateLimited and the provider falls back to FTS-only."""
        prefixed = f"{self._instruction}{text}" if self._instruction else text
        vectors = run_through_gate(
            EMBEDDING_GATE,
            settings.EMBEDDINGS_RATE_LIMIT_QUERY_MAX_WAIT_SECONDS,
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
