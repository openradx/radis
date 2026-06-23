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



@override_settings(
    EMBEDDING_BACKEND="openai",
    EMBEDDING_PROVIDER_URL="http://embed.example",
    EMBEDDING_PROVIDER_PATH="",
    EMBEDDING_PROVIDER_API_KEY="secret",
    EMBEDDING_MODEL_NAME="qwen3",
    EMBEDDING_DIM=4,
    EMBEDDING_REQUEST_TIMEOUT=10,
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
        ec, "_build_http_client", lambda: httpx.Client(transport=httpx.MockTransport(handler))
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
    EMBEDDING_QUERY_INSTRUCTION="",
)
def test_provider_path_override(monkeypatch):
    from radis.pgsearch.utils import embedding_client as ec

    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(200, json={"data": [{"embedding": [1.0, 0.0]}]})

    monkeypatch.setattr(
        ec, "_build_http_client", lambda: httpx.Client(transport=httpx.MockTransport(handler))
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
    EMBEDDING_QUERY_INSTRUCTION="INST: ",
)
def test_embed_query_prepends_instruction(monkeypatch):
    from radis.pgsearch.utils import embedding_client as ec

    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"data": [{"embedding": [1.0, 0.0]}]})

    monkeypatch.setattr(
        ec, "_build_http_client", lambda: httpx.Client(transport=httpx.MockTransport(handler))
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
    EMBEDDING_QUERY_INSTRUCTION="",
)
def test_dim_too_small_raises(monkeypatch):
    from radis.pgsearch.utils import embedding_client as ec

    def handler(request: httpx.Request) -> httpx.Response:
        # Returns dim=1, expected dim=2 -> too small, must raise.
        return httpx.Response(200, json={"data": [{"embedding": [1.0]}]})

    monkeypatch.setattr(
        ec, "_build_http_client", lambda: httpx.Client(transport=httpx.MockTransport(handler))
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
    EMBEDDING_QUERY_INSTRUCTION="",
)
def test_oversized_embedding_truncates_and_renormalizes(monkeypatch):
    from radis.pgsearch.utils import embedding_client as ec

    def handler(request: httpx.Request) -> httpx.Response:
        # Returns dim=4 ([3,4,99,99]); EMBEDDING_DIM=2 keeps [3,4], norm 5 -> [0.6, 0.8].
        return httpx.Response(200, json={"data": [{"embedding": [3.0, 4.0, 99.0, 99.0]}]})

    monkeypatch.setattr(
        ec, "_build_http_client", lambda: httpx.Client(transport=httpx.MockTransport(handler))
    )
    vectors = ec.EmbeddingClient().embed_documents(["x"])
    assert len(vectors) == 1
    assert vectors[0] == pytest.approx([0.6, 0.8])


@override_settings(
    EMBEDDING_BACKEND="openai",
    EMBEDDING_PROVIDER_URL="http://embed.example",
    EMBEDDING_PROVIDER_PATH="",
    EMBEDDING_PROVIDER_API_KEY="",
    EMBEDDING_MODEL_NAME="qwen3",
    EMBEDDING_DIM=2,
    EMBEDDING_REQUEST_TIMEOUT=10,
    EMBEDDING_QUERY_INSTRUCTION="",
)
def test_5xx_raises(monkeypatch):
    from radis.pgsearch.utils import embedding_client as ec

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="service unavailable")

    monkeypatch.setattr(
        ec, "_build_http_client", lambda: httpx.Client(transport=httpx.MockTransport(handler))
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
    EMBEDDING_QUERY_INSTRUCTION="",
)
def test_close_releases_http_client(monkeypatch):
    from radis.pgsearch.utils import embedding_client as ec

    closed = {"value": False}

    class TrackingClient:
        def post(self, *args, **kwargs):
            raise AssertionError("not used in this test")

        def close(self):
            closed["value"] = True

    monkeypatch.setattr(ec, "_build_http_client", lambda: TrackingClient())
    client = ec.EmbeddingClient()
    client.close()
    assert closed["value"] is True


@override_settings(
    EMBEDDING_BACKEND="openai",
    EMBEDDING_PROVIDER_URL="http://embed.example",
    EMBEDDING_PROVIDER_PATH="",
    EMBEDDING_PROVIDER_API_KEY="",
    EMBEDDING_MODEL_NAME="qwen3",
    EMBEDDING_DIM=2,
    EMBEDDING_REQUEST_TIMEOUT=10,
    EMBEDDING_QUERY_INSTRUCTION="",
)
def test_context_manager_closes_http_client(monkeypatch):
    from radis.pgsearch.utils import embedding_client as ec

    closed = {"value": False}

    class TrackingClient:
        def post(self, *args, **kwargs):
            raise AssertionError("not used in this test")

        def close(self):
            closed["value"] = True

    monkeypatch.setattr(ec, "_build_http_client", lambda: TrackingClient())
    with ec.EmbeddingClient():
        pass
    assert closed["value"] is True


@override_settings(
    EMBEDDING_BACKEND="openai",
    EMBEDDING_PROVIDER_URL="http://embed.example",
    EMBEDDING_PROVIDER_PATH="v1/embeddings",  # missing leading slash
    EMBEDDING_PROVIDER_API_KEY="",
    EMBEDDING_MODEL_NAME="qwen3",
    EMBEDDING_DIM=2,
    EMBEDDING_REQUEST_TIMEOUT=10,
    EMBEDDING_QUERY_INSTRUCTION="",
)
def test_provider_path_without_leading_slash_raises():
    from radis.pgsearch.utils import embedding_client as ec

    with pytest.raises(ec.EmbeddingClientError, match="must start with '/'"):
        ec.EmbeddingClient()


@override_settings(
    EMBEDDING_BACKEND="openai",
    EMBEDDING_PROVIDER_URL="http://embed.example",
    EMBEDDING_PROVIDER_PATH="",
    EMBEDDING_PROVIDER_API_KEY="",
    EMBEDDING_MODEL_NAME="qwen3",
    EMBEDDING_DIM=2,
    EMBEDDING_REQUEST_TIMEOUT=10,
    EMBEDDING_QUERY_INSTRUCTION="",
)
def test_response_count_mismatch_raises(monkeypatch):
    from radis.pgsearch.utils import embedding_client as ec

    def handler(request: httpx.Request) -> httpx.Response:
        # Requested 2 inputs, backend returns only 1.
        return httpx.Response(200, json={"data": [{"embedding": [1.0, 0.0]}]})

    monkeypatch.setattr(
        ec, "_build_http_client", lambda: httpx.Client(transport=httpx.MockTransport(handler))
    )
    with pytest.raises(ec.EmbeddingClientError, match="count mismatch"):
        ec.EmbeddingClient().embed_documents(["a", "b"])


@pytest.mark.parametrize(
    "status, body",
    [
        (413, "Payload too large"),
        (400, "This model's maximum context length is 8192 tokens, however your "
              "messages resulted in 9143 tokens"),
        (400, '{"error": {"code": "context_length_exceeded"}}'),
        (422, "input exceeds the model context"),
        (400, "request too long"),
    ],
)
def test_is_payload_too_large_detects_overlength_responses(status, body):
    from radis.pgsearch.utils.embedding_client import _is_payload_too_large

    assert _is_payload_too_large(httpx.Response(status, text=body)) is True


@pytest.mark.parametrize(
    "status, body",
    [
        (400, "missing required field 'model'"),
        (401, "invalid api key"),
        (500, "internal server error"),
        (503, "service unavailable"),
    ],
)
def test_is_payload_too_large_negatives(status, body):
    from radis.pgsearch.utils.embedding_client import _is_payload_too_large

    assert _is_payload_too_large(httpx.Response(status, text=body)) is False


@override_settings(
    EMBEDDING_BACKEND="openai",
    EMBEDDING_PROVIDER_URL="http://embed.example",
    EMBEDDING_PROVIDER_PATH="",
    EMBEDDING_PROVIDER_API_KEY="",
    EMBEDDING_MODEL_NAME="qwen3",
    EMBEDDING_DIM=2,
    EMBEDDING_REQUEST_TIMEOUT=10,
    EMBEDDING_QUERY_INSTRUCTION="",
)
def test_overlength_response_raises_typed_subclass(monkeypatch):
    from radis.pgsearch.utils import embedding_client as ec

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            text="This model's maximum context length is 8192 tokens.",
        )

    monkeypatch.setattr(
        ec, "_build_http_client", lambda: httpx.Client(transport=httpx.MockTransport(handler))
    )
    with pytest.raises(ec.EmbeddingPayloadTooLargeError):
        ec.EmbeddingClient().embed_documents(["x"])


@override_settings(
    EMBEDDING_BACKEND="openai",
    EMBEDDING_PROVIDER_URL="http://embed.example",
    EMBEDDING_PROVIDER_PATH="",
    EMBEDDING_PROVIDER_API_KEY="",
    EMBEDDING_MODEL_NAME="qwen3",
    EMBEDDING_DIM=2,
    EMBEDDING_REQUEST_TIMEOUT=10,
    EMBEDDING_QUERY_INSTRUCTION="",
)
def test_generic_4xx_still_raises_base_error(monkeypatch):
    from radis.pgsearch.utils import embedding_client as ec

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="invalid api key")

    monkeypatch.setattr(
        ec, "_build_http_client", lambda: httpx.Client(transport=httpx.MockTransport(handler))
    )
    with pytest.raises(ec.EmbeddingClientError) as excinfo:
        ec.EmbeddingClient().embed_documents(["x"])
    assert not isinstance(excinfo.value, ec.EmbeddingPayloadTooLargeError)
