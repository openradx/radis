import pytest

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
