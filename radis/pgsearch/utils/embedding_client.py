import logging

import openai
from django.conf import settings

logger = logging.getLogger(__name__)


def is_embedding_available() -> bool:
    """Check if an embedding provider is configured.

    Embeddings require an external LLM provider that supports the embeddings API.
    The local llama.cpp server used in development does not support embeddings.
    """
    return bool(settings.EXTERNAL_LLM_PROVIDER_URL)


class EmbeddingClient:
    def __init__(self) -> None:
        base_url = settings.EXTERNAL_LLM_PROVIDER_URL
        if not base_url:
            raise RuntimeError(
                "No embedding provider configured. Set EXTERNAL_LLM_PROVIDER_URL"
                " to an OpenAI-compatible API that supports embeddings."
            )
        api_key = settings.EXTERNAL_LLM_PROVIDER_API_KEY
        self._client = openai.OpenAI(base_url=base_url, api_key=api_key)
        self._model = settings.EMBEDDING_MODEL_NAME
        self._dimensions = settings.EMBEDDING_DIMENSIONS

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Generate embeddings for a batch of texts."""
        logger.debug("Generating embeddings for %d texts with model %s", len(texts), self._model)
        response = self._client.embeddings.create(
            model=self._model, input=texts, dimensions=self._dimensions
        )
        return [item.embedding for item in response.data]

    def embed_single(self, text: str) -> list[float]:
        """Generate embedding for a single text."""
        return self.embed([text])[0]
