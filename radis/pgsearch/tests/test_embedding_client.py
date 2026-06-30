import json

import httpx
import pytest
from django.test import override_settings


def _patched_settings():
    """Decorator factory: returns a single override_settings with the minimal
    config the new SDK-based client needs. Drops EMBEDDING_BACKEND and
    EMBEDDING_PROVIDER_PATH (removed in this migration)."""
    return override_settings(
        EMBEDDING_PROVIDER_URL="http://embed.example/v1",
        EMBEDDING_PROVIDER_API_KEY="secret",
        EMBEDDING_MODEL_NAME="qwen3",
        EMBEDDING_DIM=4,
        EMBEDDING_REQUEST_TIMEOUT=10,
        EMBEDDING_QUERY_INSTRUCTION="INST: ",
    )


def _install_transport(monkeypatch, handler):
    """Swap in an httpx.MockTransport via the module's _build_http_client seam.
    The returned client gets passed to openai.OpenAI(http_client=...)."""
    from radis.pgsearch.utils import embedding_client as ec

    monkeypatch.setattr(
        ec,
        "_build_http_client",
        lambda: httpx.Client(transport=httpx.MockTransport(handler)),
    )


@_patched_settings()
def test_embed_documents_posts_payload_and_normalizes(monkeypatch):
    from radis.pgsearch.utils import embedding_client as ec

    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "object": "list",
                "data": [{"object": "embedding", "index": 0, "embedding": [3.0, 0.0, 0.0, 4.0]}],
                "model": "qwen3",
                "usage": {"prompt_tokens": 1, "total_tokens": 1},
            },
        )

    _install_transport(monkeypatch, handler)
    vectors = ec.EmbeddingClient().embed_documents(["hello"])

    assert seen["url"] == "http://embed.example/v1/embeddings"
    assert seen["auth"] == "Bearer secret"
    # The SDK always sends encoding_format; we pass "float" to avoid base64 overhead.
    assert seen["body"] == {"model": "qwen3", "input": ["hello"], "encoding_format": "float"}
    # L2-normalize: original norm = 5 -> [0.6, 0, 0, 0.8].
    assert len(vectors) == 1
    assert vectors[0] == pytest.approx([0.6, 0.0, 0.0, 0.8])


@override_settings(
    EMBEDDING_PROVIDER_URL="http://embed.example/v1",
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
        return httpx.Response(
            200,
            json={
                "object": "list",
                "data": [{"object": "embedding", "index": 0, "embedding": [1.0, 0.0]}],
                "model": "qwen3",
                "usage": {"prompt_tokens": 1, "total_tokens": 1},
            },
        )

    _install_transport(monkeypatch, handler)
    ec.EmbeddingClient().embed_query("hello")
    assert seen["body"]["input"] == ["INST: hello"]


@override_settings(
    EMBEDDING_PROVIDER_URL="http://embed.example/v1",
    EMBEDDING_PROVIDER_API_KEY="",
    EMBEDDING_MODEL_NAME="qwen3",
    EMBEDDING_DIM=2,
    EMBEDDING_REQUEST_TIMEOUT=10,
    EMBEDDING_QUERY_INSTRUCTION="",
)
def test_dim_too_small_raises(monkeypatch):
    from radis.pgsearch.utils import embedding_client as ec

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "object": "list",
                "data": [{"object": "embedding", "index": 0, "embedding": [1.0]}],
                "model": "qwen3",
                "usage": {"prompt_tokens": 1, "total_tokens": 1},
            },
        )

    _install_transport(monkeypatch, handler)
    with pytest.raises(ec.EmbeddingClientError):
        ec.EmbeddingClient().embed_documents(["x"])


@override_settings(
    EMBEDDING_PROVIDER_URL="http://embed.example/v1",
    EMBEDDING_PROVIDER_API_KEY="",
    EMBEDDING_MODEL_NAME="qwen3",
    EMBEDDING_DIM=2,
    EMBEDDING_REQUEST_TIMEOUT=10,
    EMBEDDING_QUERY_INSTRUCTION="",
)
def test_oversized_embedding_truncates_and_renormalizes(monkeypatch):
    from radis.pgsearch.utils import embedding_client as ec

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "object": "list",
                "data": [{"object": "embedding", "index": 0, "embedding": [3.0, 4.0, 99.0, 99.0]}],
                "model": "qwen3",
                "usage": {"prompt_tokens": 1, "total_tokens": 1},
            },
        )

    _install_transport(monkeypatch, handler)
    vectors = ec.EmbeddingClient().embed_documents(["x"])
    assert vectors[0] == pytest.approx([0.6, 0.8])


@override_settings(
    EMBEDDING_PROVIDER_URL="http://embed.example/v1",
    EMBEDDING_PROVIDER_API_KEY="",
    EMBEDDING_MODEL_NAME="qwen3",
    EMBEDDING_DIM=2,
    EMBEDDING_REQUEST_TIMEOUT=10,
    EMBEDDING_QUERY_INSTRUCTION="",
)
def test_5xx_propagates_as_typed_openai_error(monkeypatch):
    """5xx must surface as openai.InternalServerError (not wrapped) so the
    stamina retry predicate in tasks.py can match on the typed class."""
    import openai

    from radis.pgsearch.utils import embedding_client as ec

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="service unavailable")

    _install_transport(monkeypatch, handler)
    with pytest.raises(openai.InternalServerError):
        ec.EmbeddingClient().embed_documents(["x"])


@override_settings(
    EMBEDDING_PROVIDER_URL="http://embed.example/v1",
    EMBEDDING_PROVIDER_API_KEY="",
    EMBEDDING_MODEL_NAME="qwen3",
    EMBEDDING_DIM=2,
    EMBEDDING_REQUEST_TIMEOUT=10,
    EMBEDDING_QUERY_INSTRUCTION="",
)
def test_429_propagates_as_typed_rate_limit_error(monkeypatch):
    """429 must surface as openai.RateLimitError (not wrapped) so a future
    rate-limit gate can intercept it."""
    import openai

    from radis.pgsearch.utils import embedding_client as ec

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"error": {"message": "slow down"}})

    _install_transport(monkeypatch, handler)
    with pytest.raises(openai.RateLimitError):
        ec.EmbeddingClient().embed_documents(["x"])


@override_settings(
    EMBEDDING_PROVIDER_URL="http://embed.example/v1",
    EMBEDDING_PROVIDER_API_KEY="",
    EMBEDDING_MODEL_NAME="qwen3",
    EMBEDDING_DIM=2,
    EMBEDDING_REQUEST_TIMEOUT=10,
    EMBEDDING_QUERY_INSTRUCTION="",
)
def test_too_large_via_413(monkeypatch):
    from radis.pgsearch.utils import embedding_client as ec

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(413, text="payload too large")

    _install_transport(monkeypatch, handler)
    with pytest.raises(ec.EmbeddingPayloadTooLargeError):
        ec.EmbeddingClient().embed_documents(["x"])


@override_settings(
    EMBEDDING_PROVIDER_URL="http://embed.example/v1",
    EMBEDDING_PROVIDER_API_KEY="",
    EMBEDDING_MODEL_NAME="qwen3",
    EMBEDDING_DIM=2,
    EMBEDDING_REQUEST_TIMEOUT=10,
    EMBEDDING_QUERY_INSTRUCTION="",
)
def test_too_large_via_400_with_context_length_exceeded_code(monkeypatch):
    from radis.pgsearch.utils import embedding_client as ec

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={"error": {"message": "this is too long", "code": "context_length_exceeded"}},
        )

    _install_transport(monkeypatch, handler)
    with pytest.raises(ec.EmbeddingPayloadTooLargeError):
        ec.EmbeddingClient().embed_documents(["x"])


@override_settings(
    EMBEDDING_PROVIDER_URL="http://embed.example/v1",
    EMBEDDING_PROVIDER_API_KEY="",
    EMBEDDING_MODEL_NAME="qwen3",
    EMBEDDING_DIM=2,
    EMBEDDING_REQUEST_TIMEOUT=10,
    EMBEDDING_QUERY_INSTRUCTION="",
)
def test_too_large_via_400_with_string_above_max_length_code(monkeypatch):
    from radis.pgsearch.utils import embedding_client as ec

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={"error": {"message": "too long", "code": "string_above_max_length"}},
        )

    _install_transport(monkeypatch, handler)
    with pytest.raises(ec.EmbeddingPayloadTooLargeError):
        ec.EmbeddingClient().embed_documents(["x"])


@override_settings(
    EMBEDDING_PROVIDER_URL="http://embed.example/v1",
    EMBEDDING_PROVIDER_API_KEY="",
    EMBEDDING_MODEL_NAME="qwen3",
    EMBEDDING_DIM=2,
    EMBEDDING_REQUEST_TIMEOUT=10,
    EMBEDDING_QUERY_INSTRUCTION="",
)
def test_400_with_other_code_is_not_too_large(monkeypatch):
    """400 with a non-context-length error code surfaces as
    EmbeddingClientError (not the too-large subclass)."""
    from radis.pgsearch.utils import embedding_client as ec

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={"error": {"message": "missing field", "code": "invalid_request"}},
        )

    _install_transport(monkeypatch, handler)
    with pytest.raises(ec.EmbeddingClientError) as excinfo:
        ec.EmbeddingClient().embed_documents(["x"])
    assert not isinstance(excinfo.value, ec.EmbeddingPayloadTooLargeError)


@override_settings(
    EMBEDDING_PROVIDER_URL="http://embed.example/v1",
    EMBEDDING_PROVIDER_API_KEY="",
    EMBEDDING_MODEL_NAME="qwen3",
    EMBEDDING_DIM=2,
    EMBEDDING_REQUEST_TIMEOUT=10,
    EMBEDDING_QUERY_INSTRUCTION="",
)
def test_400_with_no_error_object_is_not_too_large(monkeypatch):
    """Non-OpenAI-shaped 400 (no error.code) must NOT be classified as too-large
    — bisecting on the wrong error is worse than not bisecting."""
    from radis.pgsearch.utils import embedding_client as ec

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, text="plain text 400 from non-conforming provider")

    _install_transport(monkeypatch, handler)
    with pytest.raises(ec.EmbeddingClientError) as excinfo:
        ec.EmbeddingClient().embed_documents(["x"])
    assert not isinstance(excinfo.value, ec.EmbeddingPayloadTooLargeError)


@override_settings(
    EMBEDDING_PROVIDER_URL="",
    EMBEDDING_PROVIDER_API_KEY="",
    EMBEDDING_MODEL_NAME="qwen3",
    EMBEDDING_DIM=2,
    EMBEDDING_REQUEST_TIMEOUT=10,
    EMBEDDING_QUERY_INSTRUCTION="",
)
def test_missing_url_raises_at_construction():
    from radis.pgsearch.utils import embedding_client as ec

    with pytest.raises(ec.EmbeddingClientError, match="EMBEDDING_PROVIDER_URL"):
        ec.EmbeddingClient()


@override_settings(
    EMBEDDING_PROVIDER_URL="http://embed.example/v1",
    EMBEDDING_PROVIDER_API_KEY="",
    EMBEDDING_MODEL_NAME="qwen3",
    EMBEDDING_DIM=2,
    EMBEDDING_REQUEST_TIMEOUT=10,
    EMBEDDING_QUERY_INSTRUCTION="",
)
def test_context_manager_closes_underlying_http_client(monkeypatch):
    from radis.pgsearch.utils import embedding_client as ec

    closed = {"value": False}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "object": "list",
                "data": [],
                "model": "qwen3",
                "usage": {"prompt_tokens": 0, "total_tokens": 0},
            },
        )

    real_client = httpx.Client(transport=httpx.MockTransport(handler))
    original_close = real_client.close

    def tracking_close():
        closed["value"] = True
        original_close()

    real_client.close = tracking_close
    monkeypatch.setattr(ec, "_build_http_client", lambda: real_client)

    with ec.EmbeddingClient():
        pass
    assert closed["value"] is True
