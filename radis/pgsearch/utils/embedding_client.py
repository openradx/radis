from __future__ import annotations

import logging
import math
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


class EmbeddingClient:
    def __init__(self) -> None:
        try:
            self._backend = BACKENDS[settings.EMBEDDING_BACKEND]
        except KeyError as e:
            raise EmbeddingClientError(
                f"Unknown EMBEDDING_BACKEND={settings.EMBEDDING_BACKEND!r}; "
                f"known: {sorted(BACKENDS)}"
            ) from e
        path = settings.EMBEDDING_PROVIDER_PATH or self._backend.path
        base = settings.EMBEDDING_PROVIDER_URL.rstrip("/")
        if not base:
            raise EmbeddingClientError("EMBEDDING_PROVIDER_URL is not configured")
        self._url = f"{base}{path}"
        self._model = settings.EMBEDDING_MODEL_NAME
        self._dim = settings.EMBEDDING_DIM
        self._max_chars = settings.EMBEDDING_MAX_INPUT_CHARS
        self._instruction = settings.EMBEDDING_QUERY_INSTRUCTION
        self._headers: dict[str, str] = {}
        if settings.EMBEDDING_PROVIDER_API_KEY:
            self._headers["Authorization"] = f"Bearer {settings.EMBEDDING_PROVIDER_API_KEY}"
        self._http = _build_http_client()

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        truncated = _truncate(texts, self._max_chars)
        payload = self._backend.build_payload(self._model, truncated)
        try:
            response = self._http.post(self._url, json=payload, headers=self._headers)
        except httpx.HTTPError as e:
            raise EmbeddingClientError(f"HTTP error contacting {self._url}: {e}") from e
        if response.status_code >= 400:
            raise EmbeddingClientError(
                f"Embedding service returned {response.status_code}: {response.text[:200]}"
            )
        try:
            body = response.json()
        except ValueError as e:
            raise EmbeddingClientError(f"Embedding response is not JSON: {e}") from e
        raw = self._backend.parse_response(body)
        normalized: list[list[float]] = []
        for vec in raw:
            if len(vec) != self._dim:
                raise EmbeddingClientError(
                    f"Embedding dim mismatch: got {len(vec)}, expected {self._dim}"
                )
            normalized.append(_l2_normalize(list(vec)))
        return normalized

    def embed_query(self, text: str) -> list[float]:
        prefixed = f"{self._instruction}{text}" if self._instruction else text
        return self.embed_documents([prefixed])[0]

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "EmbeddingClient":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()
