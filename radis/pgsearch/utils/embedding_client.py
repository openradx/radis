from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Iterable, Protocol

import httpx
from django.conf import settings


class EmbeddingClientError(Exception):
    """Raised when the embedding service returns an error or a malformed response."""


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


def _build_async_http_client() -> httpx.AsyncClient:
    """Indirection so tests can swap in a MockTransport."""
    return httpx.AsyncClient(timeout=settings.EMBEDDING_REQUEST_TIMEOUT)


def _l2_normalize(vec: list[float]) -> list[float]:
    norm = math.sqrt(sum(x * x for x in vec))
    if norm == 0.0:
        return vec
    return [x / norm for x in vec]


def _truncate(texts: Iterable[str], max_chars: int) -> list[str]:
    out: list[str] = []
    for t in texts:
        if len(t) > max_chars:
            logger.warning(
                "Truncating embedding input from %d to %d chars", len(t), max_chars
            )
            out.append(t[:max_chars])
        else:
            out.append(t)
    return out


@dataclass(frozen=True)
class _ResolvedConfig:
    backend: EmbeddingBackend
    url: str
    model: str
    dim: int
    max_chars: int
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
        max_chars=settings.EMBEDDING_MAX_INPUT_CHARS,
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
        truncated_texts = _truncate(texts, self._cfg.max_chars)
        payload = self._cfg.backend.build_payload(self._cfg.model, truncated_texts)
        try:
            response = self._http.post(self._cfg.url, json=payload, headers=self._cfg.headers)
        except httpx.HTTPError as e:
            raise EmbeddingClientError(f"HTTP error contacting {self._cfg.url}: {e}") from e
        if response.status_code >= 400:
            raise EmbeddingClientError(
                f"Embedding service returned {response.status_code}: {response.text[:200]}"
            )
        try:
            body = response.json()
        except ValueError as e:
            raise EmbeddingClientError(f"Embedding response is not JSON: {e}") from e
        raw = self._cfg.backend.parse_response(body)
        return _normalize_response(raw, len(truncated_texts), self._cfg.dim)

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


class AsyncEmbeddingClient:
    """Async sibling of `EmbeddingClient` for ADRF view paths.

    Same backend protocol, same config, same response handling. Differs only
    in using `httpx.AsyncClient` and exposing `await`-able methods + an async
    context-manager lifecycle (`async with AsyncEmbeddingClient() as c:`).
    """

    def __init__(self) -> None:
        cfg = _resolve_config()
        self._cfg = cfg
        self._http = _build_async_http_client()

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        truncated_texts = _truncate(texts, self._cfg.max_chars)
        payload = self._cfg.backend.build_payload(self._cfg.model, truncated_texts)
        try:
            response = await self._http.post(
                self._cfg.url, json=payload, headers=self._cfg.headers
            )
        except httpx.HTTPError as e:
            raise EmbeddingClientError(f"HTTP error contacting {self._cfg.url}: {e}") from e
        if response.status_code >= 400:
            raise EmbeddingClientError(
                f"Embedding service returned {response.status_code}: {response.text[:200]}"
            )
        try:
            body = response.json()
        except ValueError as e:
            raise EmbeddingClientError(f"Embedding response is not JSON: {e}") from e
        raw = self._cfg.backend.parse_response(body)
        return _normalize_response(raw, len(truncated_texts), self._cfg.dim)

    async def embed_query(self, text: str) -> list[float]:
        prefixed = f"{self._cfg.instruction}{text}" if self._cfg.instruction else text
        vectors = await self.embed_documents([prefixed])
        if not vectors:
            raise EmbeddingClientError("Embedding service returned no vectors for query")
        return vectors[0]

    async def aclose(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> "AsyncEmbeddingClient":
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        await self.aclose()
