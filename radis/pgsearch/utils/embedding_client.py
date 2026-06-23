from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Protocol

import httpx
from django.conf import settings


class EmbeddingClientError(Exception):
    """Raised when the embedding service returns an error or a malformed response."""


class EmbeddingPayloadTooLargeError(EmbeddingClientError):
    """Raised when the backend rejects a request because one or more inputs
    exceed the model's context window. Callers can bisect the batch and
    retry — `embed_reports_task` does exactly that."""


class EmbeddingBackend(Protocol):
    path: str

    def build_payload(self, model: str, texts: list[str]) -> dict: ...

    def parse_response(self, body: dict) -> list[list[float]]: ...


class OpenAIBackend:
    path: str = "/v1/embeddings"

    def build_payload(self, model: str, texts: list[str]) -> dict:
        return {"model": model, "input": texts}

    def parse_response(self, body: dict) -> list[list[float]]:
        try:
            return [item["embedding"] for item in body["data"]]
        except (KeyError, TypeError) as e:
            raise EmbeddingClientError(
                f"OpenAI-style response missing 'data[*].embedding': {e}"
            ) from e


class OllamaBackend:
    path: str = "/api/embed"

    def build_payload(self, model: str, texts: list[str]) -> dict:
        return {"model": model, "input": texts}

    def parse_response(self, body: dict) -> list[list[float]]:
        try:
            return list(body["embeddings"])
        except (KeyError, TypeError) as e:
            raise EmbeddingClientError(
                f"Ollama-style response missing 'embeddings': {e}"
            ) from e


BACKENDS: dict[str, EmbeddingBackend] = {
    "openai": OpenAIBackend(),
    "ollama": OllamaBackend(),
}

logger = logging.getLogger(__name__)


def _build_http_client() -> httpx.Client:
    """Indirection so tests can swap in a MockTransport."""
    return httpx.Client(timeout=settings.EMBEDDING_REQUEST_TIMEOUT)


def _l2_normalize(vec: list[float]) -> list[float]:
    norm = math.sqrt(sum(x * x for x in vec))
    if norm == 0.0:
        return vec
    return [x / norm for x in vec]


# Substrings (case-insensitive) seen in embedding-service responses when one
# or more inputs exceed the model's context window. Kept loose because the
# exact phrasing varies across OpenAI / vLLM / Ollama and minor version bumps.
_TOO_LARGE_MARKERS = (
    "context length",
    "context_length",
    "maximum context",
    "max_tokens",
    "max tokens",
    "max_position",
    "too long",
    "too large",
    "too many tokens",
    "exceeds",
    "exceeded",
)


def _is_payload_too_large(response: httpx.Response) -> bool:
    """Best-effort detection: is this 4xx caused by an input exceeding the
    model's context window (i.e., bisecting the batch could resolve it)?"""
    if response.status_code == 413:
        return True
    if response.status_code not in (400, 422):
        return False
    body_lower = response.text.lower()
    return any(marker in body_lower for marker in _TOO_LARGE_MARKERS)


@dataclass(frozen=True)
class _ResolvedConfig:
    backend: EmbeddingBackend
    url: str
    model: str
    dim: int
    instruction: str
    headers: dict[str, str]


def _resolve_config() -> _ResolvedConfig:
    """Read+validate Django settings once; raise EmbeddingClientError on misconfig."""
    try:
        backend = BACKENDS[settings.EMBEDDING_BACKEND]
    except KeyError as e:
        raise EmbeddingClientError(
            f"Unknown EMBEDDING_BACKEND={settings.EMBEDDING_BACKEND!r}; "
            f"known: {sorted(BACKENDS)}"
        ) from e
    path = settings.EMBEDDING_PROVIDER_PATH or backend.path
    if not path.startswith("/"):
        raise EmbeddingClientError(
            f"EMBEDDING_PROVIDER_PATH must start with '/'; got {path!r}"
        )
    base = settings.EMBEDDING_PROVIDER_URL.rstrip("/")
    if not base:
        raise EmbeddingClientError("EMBEDDING_PROVIDER_URL is not configured")
    headers: dict[str, str] = {}
    if settings.EMBEDDING_PROVIDER_API_KEY:
        headers["Authorization"] = f"Bearer {settings.EMBEDDING_PROVIDER_API_KEY}"
    return _ResolvedConfig(
        backend=backend,
        url=f"{base}{path}",
        model=settings.EMBEDDING_MODEL_NAME,
        dim=settings.EMBEDDING_DIM,
        instruction=settings.EMBEDDING_QUERY_INSTRUCTION,
        headers=headers,
    )


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
            # Matryoshka truncation: keep first EMBEDDING_DIM components, then re-normalize.
            # Qwen3-Embedding is trained to retain quality at truncated dimensions.
            normalized.append(_l2_normalize(list(vec[:target_dim])))
        else:
            # Length already matches; still normalize since we can't assume
            # all providers return unit vectors.
            normalized.append(_l2_normalize(list(vec)))
    return normalized


class EmbeddingClient:
    def __init__(self) -> None:
        cfg = _resolve_config()
        self._cfg = cfg
        self._http = _build_http_client()

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        payload = self._cfg.backend.build_payload(self._cfg.model, texts)
        try:
            response = self._http.post(self._cfg.url, json=payload, headers=self._cfg.headers)
        except httpx.HTTPError as e:
            raise EmbeddingClientError(f"HTTP error contacting {self._cfg.url}: {e}") from e
        if response.status_code >= 400:
            snippet = response.text[:200]
            if _is_payload_too_large(response):
                raise EmbeddingPayloadTooLargeError(
                    f"Embedding service rejected payload as too large "
                    f"({response.status_code}): {snippet}"
                )
            raise EmbeddingClientError(
                f"Embedding service returned {response.status_code}: {snippet}"
            )
        try:
            body = response.json()
        except ValueError as e:
            raise EmbeddingClientError(f"Embedding response is not JSON: {e}") from e
        raw = self._cfg.backend.parse_response(body)
        return _normalize_response(raw, len(texts), self._cfg.dim)

    def embed_query(self, text: str) -> list[float]:
        prefixed = f"{self._cfg.instruction}{text}" if self._cfg.instruction else text
        vectors = self.embed_documents([prefixed])
        if not vectors:
            raise EmbeddingClientError("Embedding service returned no vectors for query")
        return vectors[0]

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "EmbeddingClient":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()
