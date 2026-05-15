import json

import httpx
import pytest
from django.test import override_settings

from radis.pgsearch.utils.embedding_client import (
    BACKENDS,
    OllamaBackend,
    OpenAIBackend,
)


def test_openai_backend_builds_payload():
    backend = OpenAIBackend()
    payload = backend.build_payload(model="m1", texts=["a", "b"])
    assert payload == {"model": "m1", "input": ["a", "b"]}


def test_openai_backend_default_path():
    assert OpenAIBackend().path == "/v1/embeddings"


def test_openai_backend_parses_response():
    backend = OpenAIBackend()
    body = {"data": [{"embedding": [0.1, 0.2]}, {"embedding": [0.3, 0.4]}]}
    assert backend.parse_response(body) == [[0.1, 0.2], [0.3, 0.4]]


def test_openai_backend_parse_raises_on_missing_data_key():
    from radis.pgsearch.utils.embedding_client import EmbeddingClientError

    backend = OpenAIBackend()
    with pytest.raises(EmbeddingClientError):
        backend.parse_response({"oops": []})


def test_ollama_backend_builds_payload():
    backend = OllamaBackend()
    payload = backend.build_payload(model="m1", texts=["a", "b"])
    assert payload == {"model": "m1", "input": ["a", "b"]}


def test_ollama_backend_default_path():
    assert OllamaBackend().path == "/api/embed"


def test_ollama_backend_parses_response():
    backend = OllamaBackend()
    body = {"embeddings": [[0.1, 0.2], [0.3, 0.4]]}
    assert backend.parse_response(body) == [[0.1, 0.2], [0.3, 0.4]]


def test_ollama_backend_parse_raises_on_missing_key():
    from radis.pgsearch.utils.embedding_client import EmbeddingClientError

    backend = OllamaBackend()
    with pytest.raises(EmbeddingClientError):
        backend.parse_response({"data": []})


def test_backends_registry_keys():
    assert set(BACKENDS.keys()) == {"openai", "ollama"}


def _mock_transport(handler):
    """Build an httpx MockTransport that delegates to a handler(request)."""
    return httpx.MockTransport(handler)


@override_settings(
    EMBEDDING_BACKEND="openai",
    EMBEDDING_PROVIDER_URL="http://embed.example",
    EMBEDDING_PROVIDER_PATH="",
    EMBEDDING_PROVIDER_API_KEY="secret",
    EMBEDDING_MODEL_NAME="qwen3",
    EMBEDDING_DIM=4,
    EMBEDDING_REQUEST_TIMEOUT=10,
    EMBEDDING_MAX_INPUT_CHARS=100,
    EMBEDDING_QUERY_INSTRUCTION="INST: ",
)
def test_embed_documents_posts_payload_and_normalizes(monkeypatch):
    from radis.pgsearch.utils import embedding_client as ec

    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200, json={"data": [{"embedding": [3.0, 0.0, 0.0, 4.0]}]}
        )

    monkeypatch.setattr(
        ec, "_build_http_client", lambda: httpx.Client(transport=_mock_transport(handler))
    )

    client = ec.EmbeddingClient()
    vectors = client.embed_documents(["hello"])

    assert seen["url"] == "http://embed.example/v1/embeddings"
    assert seen["auth"] == "Bearer secret"
    assert seen["body"] == {"model": "qwen3", "input": ["hello"]}
    # L2-normalized: original norm = 5, normalized = [0.6, 0, 0, 0.8]
    assert len(vectors) == 1
    assert vectors[0] == pytest.approx([0.6, 0.0, 0.0, 0.8])


@override_settings(
    EMBEDDING_BACKEND="openai",
    EMBEDDING_PROVIDER_URL="http://embed.example",
    EMBEDDING_PROVIDER_PATH="/api/embeddings",
    EMBEDDING_PROVIDER_API_KEY="",
    EMBEDDING_MODEL_NAME="qwen3",
    EMBEDDING_DIM=2,
    EMBEDDING_REQUEST_TIMEOUT=10,
    EMBEDDING_MAX_INPUT_CHARS=100,
    EMBEDDING_QUERY_INSTRUCTION="",
)
def test_provider_path_override(monkeypatch):
    from radis.pgsearch.utils import embedding_client as ec

    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(200, json={"data": [{"embedding": [1.0, 0.0]}]})

    monkeypatch.setattr(
        ec, "_build_http_client", lambda: httpx.Client(transport=_mock_transport(handler))
    )
    ec.EmbeddingClient().embed_documents(["x"])
    assert seen["url"] == "http://embed.example/api/embeddings"


@override_settings(
    EMBEDDING_BACKEND="openai",
    EMBEDDING_PROVIDER_URL="http://embed.example",
    EMBEDDING_PROVIDER_PATH="",
    EMBEDDING_PROVIDER_API_KEY="",
    EMBEDDING_MODEL_NAME="qwen3",
    EMBEDDING_DIM=2,
    EMBEDDING_REQUEST_TIMEOUT=10,
    EMBEDDING_MAX_INPUT_CHARS=100,
    EMBEDDING_QUERY_INSTRUCTION="INST: ",
)
def test_embed_query_prepends_instruction(monkeypatch):
    from radis.pgsearch.utils import embedding_client as ec

    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"data": [{"embedding": [1.0, 0.0]}]})

    monkeypatch.setattr(
        ec, "_build_http_client", lambda: httpx.Client(transport=_mock_transport(handler))
    )
    ec.EmbeddingClient().embed_query("hello")
    assert seen["body"]["input"] == ["INST: hello"]


@override_settings(
    EMBEDDING_BACKEND="openai",
    EMBEDDING_PROVIDER_URL="http://embed.example",
    EMBEDDING_PROVIDER_PATH="",
    EMBEDDING_PROVIDER_API_KEY="",
    EMBEDDING_MODEL_NAME="qwen3",
    EMBEDDING_DIM=2,
    EMBEDDING_REQUEST_TIMEOUT=10,
    EMBEDDING_MAX_INPUT_CHARS=5,
    EMBEDDING_QUERY_INSTRUCTION="",
)
def test_truncates_long_input(monkeypatch):
    from radis.pgsearch.utils import embedding_client as ec

    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"data": [{"embedding": [1.0, 0.0]}]})

    monkeypatch.setattr(
        ec, "_build_http_client", lambda: httpx.Client(transport=_mock_transport(handler))
    )
    ec.EmbeddingClient().embed_documents(["abcdefghij"])
    assert seen["body"]["input"] == ["abcde"]


@override_settings(
    EMBEDDING_BACKEND="openai",
    EMBEDDING_PROVIDER_URL="http://embed.example",
    EMBEDDING_PROVIDER_PATH="",
    EMBEDDING_PROVIDER_API_KEY="",
    EMBEDDING_MODEL_NAME="qwen3",
    EMBEDDING_DIM=2,
    EMBEDDING_REQUEST_TIMEOUT=10,
    EMBEDDING_MAX_INPUT_CHARS=100,
    EMBEDDING_QUERY_INSTRUCTION="",
)
def test_dim_mismatch_raises(monkeypatch):
    from radis.pgsearch.utils import embedding_client as ec

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [{"embedding": [1.0, 0.0, 3.0]}]})

    monkeypatch.setattr(
        ec, "_build_http_client", lambda: httpx.Client(transport=_mock_transport(handler))
    )
    with pytest.raises(ec.EmbeddingClientError):
        ec.EmbeddingClient().embed_documents(["x"])


@override_settings(
    EMBEDDING_BACKEND="openai",
    EMBEDDING_PROVIDER_URL="http://embed.example",
    EMBEDDING_PROVIDER_PATH="",
    EMBEDDING_PROVIDER_API_KEY="",
    EMBEDDING_MODEL_NAME="qwen3",
    EMBEDDING_DIM=2,
    EMBEDDING_REQUEST_TIMEOUT=10,
    EMBEDDING_MAX_INPUT_CHARS=100,
    EMBEDDING_QUERY_INSTRUCTION="",
)
def test_5xx_raises(monkeypatch):
    from radis.pgsearch.utils import embedding_client as ec

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="service unavailable")

    monkeypatch.setattr(
        ec, "_build_http_client", lambda: httpx.Client(transport=_mock_transport(handler))
    )
    with pytest.raises(ec.EmbeddingClientError):
        ec.EmbeddingClient().embed_documents(["x"])
