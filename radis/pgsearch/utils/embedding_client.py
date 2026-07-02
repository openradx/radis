from __future__ import annotations

import logging
import math

import httpx
import openai
from django.conf import settings

from .rate_limiter import call_with_429_backoff

logger = logging.getLogger(__name__)


class EmbeddingClientError(Exception):
    """Raised when the embedding service returns a malformed response or when
    configuration is invalid. Typed `openai.OpenAIError` subclasses
    (RateLimitError, BadRequestError, InternalServerError, ...) are NOT wrapped
    in this class — callers that want to discriminate (stamina retry predicate,
    429 backoff) match on the SDK types directly."""


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
            max_retries=0,  # 429s are handled by call_with_429_backoff, not the SDK
            timeout=settings.EMBEDDING_REQUEST_TIMEOUT,
        )
        self._model = settings.EMBEDDING_MODEL_NAME
        self._dim = settings.EMBEDDING_DIM
        self._instruction = settings.EMBEDDING_QUERY_INSTRUCTION

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """Low-level call to the embedding backend, with no 429 handling of
        its own — `embed_query` (below) and `_embed_chunk_with_retry` in
        `radis/pgsearch/tasks.py` wrap it in `call_with_429_backoff`. HTTP
        errors (400, 429, 5xx, ...) propagate as typed SDK exceptions."""
        # encoding_format="float" requests JSON-float vectors. Without this
        # the SDK defaults to base64, which would require a decode step
        # back to floats — extra work and a less debuggable wire format.
        response = self._client.embeddings.create(
            model=self._model, input=texts, encoding_format="float"
        )
        raw = [list(item.embedding) for item in response.data]
        return _normalize_response(raw, len(texts), self._dim)

    def embed_query(self, text: str) -> list[float]:
        prefixed = f"{self._instruction}{text}" if self._instruction else text
        vectors = call_with_429_backoff(
            lambda: self.embed_documents([prefixed]), shared_gate=False
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
