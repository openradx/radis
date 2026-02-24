from unittest.mock import MagicMock, patch

import pytest

from radis.pgsearch.utils.embedding_client import EmbeddingClient, is_embedding_available


def test_embed_single(settings):
    settings.EXTERNAL_LLM_PROVIDER_URL = "http://test-llm:8000/v1"
    settings.EXTERNAL_LLM_PROVIDER_API_KEY = "test-key"
    settings.EMBEDDING_MODEL_NAME = "text-embedding-3-large"
    settings.EMBEDDING_DIMENSIONS = 1536

    mock_response = MagicMock()
    mock_item = MagicMock()
    mock_item.embedding = [0.1, 0.2, 0.3]
    mock_response.data = [mock_item]

    with patch("radis.pgsearch.utils.embedding_client.openai.OpenAI") as mock_openai_cls:
        mock_client = MagicMock()
        mock_openai_cls.return_value = mock_client
        mock_client.embeddings.create.return_value = mock_response

        client = EmbeddingClient()
        result = client.embed_single("test text")

        assert result == [0.1, 0.2, 0.3]
        mock_client.embeddings.create.assert_called_once_with(
            model="text-embedding-3-large", input=["test text"], dimensions=1536
        )


def test_embed_batch(settings):
    settings.EXTERNAL_LLM_PROVIDER_URL = "http://test-llm:8000/v1"
    settings.EXTERNAL_LLM_PROVIDER_API_KEY = "test-key"
    settings.EMBEDDING_MODEL_NAME = "text-embedding-3-large"
    settings.EMBEDDING_DIMENSIONS = 1536

    mock_response = MagicMock()
    mock_item1 = MagicMock()
    mock_item1.embedding = [0.1, 0.2, 0.3]
    mock_item2 = MagicMock()
    mock_item2.embedding = [0.4, 0.5, 0.6]
    mock_response.data = [mock_item1, mock_item2]

    with patch("radis.pgsearch.utils.embedding_client.openai.OpenAI") as mock_openai_cls:
        mock_client = MagicMock()
        mock_openai_cls.return_value = mock_client
        mock_client.embeddings.create.return_value = mock_response

        client = EmbeddingClient()
        result = client.embed(["text one", "text two"])

        assert result == [[0.1, 0.2, 0.3], [0.4, 0.5, 0.6]]
        mock_client.embeddings.create.assert_called_once_with(
            model="text-embedding-3-large", input=["text one", "text two"], dimensions=1536
        )


def test_embed_uses_correct_base_url(settings):
    settings.EXTERNAL_LLM_PROVIDER_URL = "http://my-provider:9000/v1"
    settings.EXTERNAL_LLM_PROVIDER_API_KEY = "my-key"
    settings.EMBEDDING_MODEL_NAME = "test-model"
    settings.EMBEDDING_DIMENSIONS = 1536

    with patch("radis.pgsearch.utils.embedding_client.openai.OpenAI") as mock_openai_cls:
        mock_client = MagicMock()
        mock_openai_cls.return_value = mock_client

        EmbeddingClient()

        mock_openai_cls.assert_called_once_with(
            base_url="http://my-provider:9000/v1", api_key="my-key"
        )


def test_raises_when_no_provider_configured(settings):
    settings.EXTERNAL_LLM_PROVIDER_URL = ""

    with pytest.raises(RuntimeError, match="No embedding provider configured"):
        EmbeddingClient()


def test_is_embedding_available_true(settings):
    settings.EXTERNAL_LLM_PROVIDER_URL = "http://provider:8000/v1"
    assert is_embedding_available() is True


def test_is_embedding_available_false(settings):
    settings.EXTERNAL_LLM_PROVIDER_URL = ""
    assert is_embedding_available() is False
