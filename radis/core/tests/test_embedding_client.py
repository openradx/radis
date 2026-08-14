import json

import httpx
import pytest
from django.test import override_settings

from radis.core.utils.model_spec import parse_model_spec


def _patched_settings():
    """Decorator factory: a single override_settings with the minimal
    config the SDK-based client reads."""
    return override_settings(
        EMBEDDINGS_BASE_URL="http://embed.example/v1",
        EMBEDDINGS_API_KEY="secret",
        EMBEDDINGS_MODEL=parse_model_spec("qwen3"),
        EMBEDDINGS_DIM=4,
        EMBEDDINGS_REQUEST_TIMEOUT_SECONDS=10.0,
        EMBEDDINGS_QUERY_INSTRUCTION="INST: ",
    )


def _install_transport(monkeypatch, handler):
    """Swap in an httpx.MockTransport via the module's _build_http_client seam.
    The returned client gets passed to openai.OpenAI(http_client=...)."""
    from radis.core.utils import embedding_client as ec

    monkeypatch.setattr(
        ec,
        "_build_http_client",
        lambda: httpx.Client(transport=httpx.MockTransport(handler)),
    )


@pytest.fixture(autouse=True)
def _bypass_rate_limit_gate(monkeypatch):
    """These tests exercise EmbeddingClient's request/response handling, not
    the rate-limit gate itself (covered in radis/core/tests/test_rate_limit.py).
    Patch the gate runner to a passthrough so a stray 429 in a test double
    can't trigger real sleeps, and reset the process-global gate so a 429
    armed by one test can't leak a closed window into another."""
    from radis.core.utils import embedding_client as ec

    ec.EMBEDDING_GATE.reset()
    monkeypatch.setattr(ec, "run_through_gate", lambda gate, budget, fn: fn())


@_patched_settings()
def test_embed_documents_posts_payload_and_normalizes(monkeypatch):
    from radis.core.utils import embedding_client as ec

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
    EMBEDDINGS_BASE_URL="http://embed.example/v1",
    EMBEDDINGS_API_KEY="",
    EMBEDDINGS_MODEL=parse_model_spec("qwen3"),
    EMBEDDINGS_DIM=2,
    EMBEDDINGS_REQUEST_TIMEOUT_SECONDS=10.0,
    EMBEDDINGS_QUERY_INSTRUCTION="INST: ",
)
def test_embed_query_prepends_instruction(monkeypatch):
    from radis.core.utils import embedding_client as ec

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
    EMBEDDINGS_BASE_URL="http://embed.example/v1",
    EMBEDDINGS_API_KEY="",
    EMBEDDINGS_MODEL=parse_model_spec("qwen3"),
    EMBEDDINGS_DIM=2,
    EMBEDDINGS_REQUEST_TIMEOUT_SECONDS=10.0,
    EMBEDDINGS_QUERY_INSTRUCTION="",
)
def test_dim_too_small_raises(monkeypatch):
    from radis.core.utils import embedding_client as ec

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
    EMBEDDINGS_BASE_URL="http://embed.example/v1",
    EMBEDDINGS_API_KEY="",
    EMBEDDINGS_MODEL=parse_model_spec("qwen3"),
    EMBEDDINGS_DIM=2,
    EMBEDDINGS_REQUEST_TIMEOUT_SECONDS=10.0,
    EMBEDDINGS_QUERY_INSTRUCTION="",
)
def test_oversized_embedding_truncates_and_renormalizes(monkeypatch):
    from radis.core.utils import embedding_client as ec

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
    EMBEDDINGS_BASE_URL="http://embed.example/v1",
    EMBEDDINGS_API_KEY="",
    EMBEDDINGS_MODEL=parse_model_spec("qwen3"),
    EMBEDDINGS_DIM=2,
    EMBEDDINGS_REQUEST_TIMEOUT_SECONDS=10.0,
    EMBEDDINGS_QUERY_INSTRUCTION="",
)
def test_5xx_propagates_as_typed_openai_error(monkeypatch):
    """5xx must surface as openai.InternalServerError (not wrapped) so the
    transient retry layer in tasks.py can match on the typed class."""
    import openai

    from radis.core.utils import embedding_client as ec

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="service unavailable")

    _install_transport(monkeypatch, handler)
    with pytest.raises(openai.InternalServerError):
        ec.EmbeddingClient().embed_documents(["x"])


@override_settings(
    EMBEDDINGS_BASE_URL="http://embed.example/v1",
    EMBEDDINGS_API_KEY="",
    EMBEDDINGS_MODEL=parse_model_spec("qwen3"),
    EMBEDDINGS_DIM=2,
    EMBEDDINGS_REQUEST_TIMEOUT_SECONDS=10.0,
    EMBEDDINGS_QUERY_INSTRUCTION="",
)
def test_429_propagates_as_typed_rate_limit_error(monkeypatch):
    """429 must surface as openai.RateLimitError (not wrapped) so the
    rate-limit gate can intercept it."""
    import openai

    from radis.core.utils import embedding_client as ec

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"error": {"message": "slow down"}})

    _install_transport(monkeypatch, handler)
    with pytest.raises(openai.RateLimitError):
        ec.EmbeddingClient().embed_documents(["x"])


@override_settings(
    EMBEDDINGS_BASE_URL="http://embed.example/v1",
    EMBEDDINGS_API_KEY="",
    EMBEDDINGS_MODEL=parse_model_spec("qwen3"),
    EMBEDDINGS_DIM=2,
    EMBEDDINGS_REQUEST_TIMEOUT_SECONDS=10.0,
    EMBEDDINGS_QUERY_INSTRUCTION="",
)
def test_400_propagates_as_typed_bad_request_error(monkeypatch):
    """4xx client errors surface as typed SDK exceptions (not wrapped) so
    they escape the transient retry layer and fail the subjob fast."""
    import openai

    from radis.core.utils import embedding_client as ec

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={"error": {"message": "invalid model", "param": "model", "code": 400}},
        )

    _install_transport(monkeypatch, handler)
    with pytest.raises(openai.BadRequestError):
        ec.EmbeddingClient().embed_documents(["x"])


@_patched_settings()
def test_embed_query_runs_through_gate_with_query_budget(monkeypatch):
    from django.conf import settings

    from radis.core.utils import embedding_client as ec

    seen = {}

    def fake_run_through_gate(gate, budget, fn):
        seen["gate"] = gate
        seen["budget"] = budget
        return fn()

    monkeypatch.setattr(ec, "run_through_gate", fake_run_through_gate)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [{"embedding": [1.0, 0.0, 0.0, 0.0]}]})

    _install_transport(monkeypatch, handler)

    vec = ec.EmbeddingClient().embed_query("pneumonia")

    assert vec == [1.0, 0.0, 0.0, 0.0]
    assert seen["gate"] is ec.EMBEDDING_GATE, "embed_query must use the shared embedding gate"
    assert seen["budget"] == settings.EMBEDDINGS_RATE_LIMIT_QUERY_MAX_WAIT_SECONDS, (
        "a user is waiting on search, so embed_query gets the short query budget"
    )


@override_settings(
    EMBEDDINGS_BASE_URL="http://embed.example/v1",
    EMBEDDINGS_API_KEY="",
    EMBEDDINGS_MODEL=parse_model_spec("qwen3"),
    EMBEDDINGS_DIM=2,
    EMBEDDINGS_REQUEST_TIMEOUT_SECONDS=10.0,
    EMBEDDINGS_QUERY_INSTRUCTION="",
    EMBEDDINGS_RATE_LIMIT_QUERY_MAX_WAIT_SECONDS=0.0,
)
def test_429_through_real_gate_raises_rate_limited_and_arms_gate(monkeypatch):
    """End-to-end: a gateway 429 travels MockTransport → typed SDK
    RateLimitError → the real run_through_gate, which arms EMBEDDING_GATE
    and — with a zero query budget — defers immediately via RateLimited
    instead of sleeping out the 30s Retry-After."""
    import time

    from radis.core.utils import embedding_client as ec
    from radis.core.utils.rate_limit import RateLimited, run_through_gate

    # Undo the autouse passthrough: this test exercises the real gate wiring.
    monkeypatch.setattr(ec, "run_through_gate", run_through_gate)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429,
            headers={"retry-after": "30"},
            json={"error": {"message": "slow down"}},
        )

    _install_transport(monkeypatch, handler)
    with pytest.raises(RateLimited):
        ec.EmbeddingClient().embed_query("pneumonia")

    # The 429 armed the shared gate: it reports closed to a caller whose
    # deadline is now. (The autouse fixture resets the gate for later tests.)
    assert ec.EMBEDDING_GATE.wait_until_open(deadline=time.monotonic()) is False


@override_settings(EMBEDDINGS_MODEL=None)
def test_construction_fails_fast_when_no_model_is_configured():
    from radis.core.utils import embedding_client as ec

    with pytest.raises(ec.EmbeddingClientError, match="EMBEDDINGS_MODEL"):
        ec.EmbeddingClient()


@override_settings(
    EMBEDDINGS_BASE_URL="http://embed.example/v1",
    EMBEDDINGS_API_KEY="",
    EMBEDDINGS_MODEL=parse_model_spec("qwen3"),
    EMBEDDINGS_DIM=2,
    EMBEDDINGS_REQUEST_TIMEOUT_SECONDS=10.0,
    EMBEDDINGS_QUERY_INSTRUCTION="",
)
def test_context_manager_closes_underlying_http_client(monkeypatch):
    from radis.core.utils import embedding_client as ec

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


@override_settings(
    EMBEDDINGS_BASE_URL="http://embed.example/v1",
    EMBEDDINGS_API_KEY="",
    EMBEDDINGS_MODEL=parse_model_spec("text-embedding-3-large?dimensions=2"),
    EMBEDDINGS_DIM=2,
    EMBEDDINGS_REQUEST_TIMEOUT_SECONDS=10.0,
    EMBEDDINGS_QUERY_INSTRUCTION="",
)
def test_model_spec_parameters_reach_the_request_body(monkeypatch):
    from radis.core.utils import embedding_client as ec

    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "object": "list",
                "data": [{"object": "embedding", "index": 0, "embedding": [1.0, 0.0]}],
                "model": "text-embedding-3-large",
                "usage": {"prompt_tokens": 1, "total_tokens": 1},
            },
        )

    _install_transport(monkeypatch, handler)
    ec.EmbeddingClient().embed_documents(["hello"])

    assert seen["body"]["model"] == "text-embedding-3-large"
    # `dimensions` is a real OpenAI request field: asking the provider for the width we
    # store beats truncating a larger vector client-side.
    assert seen["body"]["dimensions"] == 2
