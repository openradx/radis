from __future__ import annotations

from typing import Protocol


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
