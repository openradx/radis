from unittest.mock import patch

import pytest
from django.contrib.auth.models import Group
from django.test import override_settings

from radis.core.utils.embedding_client import EmbeddingClientError
from radis.core.utils.model_spec import parse_model_spec
from radis.pgsearch import providers
from radis.pgsearch.models import ReportSearchIndex
from radis.pgsearch.providers import search
from radis.reports.factories import ReportFactory
from radis.search.site import FullTextSearch, SearchFilters, SemanticSearch, SimilarityRerank
from radis.search.utils.query_parser import QueryParser

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _embedding_settings(settings):
    """These tests exercise the cache wrapper around a configured embedding model.
    Test settings default EMBEDDINGS_MODEL to None (no embedding model, full-text
    only), so default it here; tests proving the model/params participate in the key
    override it explicitly. The cache lifetime is pinned because a developer's .env
    reaches test runs outside Docker."""
    settings.EMBEDDINGS_MODEL = parse_model_spec("qwen3")
    settings.EMBEDDINGS_QUERY_CACHE_TIMEOUT_SECONDS = 900


def _unit_vec(idx: int, dim: int) -> list[float]:
    v = [0.0] * dim
    v[idx % dim] = 1.0
    return v


def _make_search(description: str, group_id: int, offset: int = 0) -> FullTextSearch:
    node, _ = QueryParser().parse("pneumothorax")
    assert node is not None
    return FullTextSearch(
        query=node,
        # language="en" matches the report's pinned language below, so the
        # query-side tsquery config equals the config the report was indexed
        # with (ReportFactory otherwise randomizes the language, and a config
        # mismatch makes lexical matching flaky; see test_provider_fulltext.py).
        filters=SearchFilters(group=group_id, language="en"),
        rerank=SimilarityRerank(description),
        offset=offset,
        limit=25,
    )


@pytest.fixture
def group(db):
    return Group.objects.create(name="radiology")


@pytest.fixture
def report(group, settings):
    """One keyword hit with an embedding, so its similarity proves the description's
    vector was actually used (not just that no exception occurred)."""
    r = ReportFactory.create(body="Findings: pneumothorax on the left.", language__code="en")
    r.groups.add(group)
    ReportSearchIndex.objects.filter(report=r).update(
        embedding=_unit_vec(0, settings.EMBEDDINGS_DIM)
    )
    return r


def _mock_embedding_client(dim: int):
    patcher = patch("radis.pgsearch.providers.EmbeddingClient")
    MockClient = patcher.start()
    MockClient.return_value.__enter__.return_value = MockClient.return_value
    MockClient.return_value.__exit__.return_value = None
    MockClient.return_value.embed_query.return_value = _unit_vec(0, dim)
    return patcher, MockClient.return_value.embed_query


def _similarity(result, report) -> float | None:
    return next(d.similarity for d in result.documents if d.document_id == report.document_id)


def test_paginated_search_embeds_the_description_only_once(group, report, settings):
    # With the ranking cache off, only the query-embedding cache can spare the second call.
    settings.SEARCH_RANKING_CACHE_TIMEOUT_SECONDS = 0
    patcher, embed_query = _mock_embedding_client(settings.EMBEDDINGS_DIM)
    try:
        first = search(_make_search("free air", group.pk, offset=0))
        second = search(_make_search("free air", group.pk, offset=0))
    finally:
        patcher.stop()

    assert embed_query.call_count == 1
    # The vector was really used: the report carries its similarity both times.
    for result in (first, second):
        assert _similarity(result, report) == pytest.approx(1.0)


def test_embedding_failure_is_not_cached(group, report, settings):
    """A transient embedding outage must not pin later searches to a degraded result."""
    patcher, embed_query = _mock_embedding_client(settings.EMBEDDINGS_DIM)
    embed_query.side_effect = [
        EmbeddingClientError("down"),
        _unit_vec(0, settings.EMBEDDINGS_DIM),
    ]
    try:
        first = search(_make_search("free air", group.pk))
        second = search(_make_search("free air", group.pk))
    finally:
        patcher.stop()

    assert embed_query.call_count == 2
    assert first.degraded is True
    assert second.degraded is False
    assert _similarity(second, report) == pytest.approx(1.0)


def test_different_descriptions_embed_separately(group, report, settings):
    patcher, embed_query = _mock_embedding_client(settings.EMBEDDINGS_DIM)
    try:
        search(_make_search("free air", group.pk))
        search(_make_search("collapsed lung", group.pk))
    finally:
        patcher.stop()

    assert embed_query.call_count == 2


def test_differing_spec_parameters_do_not_share_a_cache_entry(monkeypatch):
    # dimensions=2 and dimensions=4 are different vectors for the same model text.
    calls = []

    def fake_embed(text, caller):
        calls.append(text)
        return [1.0, 0.0]

    monkeypatch.setattr(providers, "_embed_query_or_none", fake_embed)

    with override_settings(EMBEDDINGS_MODEL=parse_model_spec("qwen3?dimensions=2")):
        providers._embed_query_cached("pneumonia", "test")
    with override_settings(EMBEDDINGS_MODEL=parse_model_spec("qwen3?dimensions=4")):
        providers._embed_query_cached("pneumonia", "test")

    assert len(calls) == 2


def test_differing_base_url_does_not_share_a_cache_entry(monkeypatch):
    # Same model name, two deployments -- "qwen3" on one gateway is different weights
    # (and thus a different vector) than "qwen3" on another, so the endpoint must be
    # part of the fingerprint or a repointed deployment would serve stale vectors from
    # the shared (database-backed, restart-surviving) cache.
    calls = []

    def fake_embed(text, caller):
        calls.append(text)
        return [1.0, 0.0]

    monkeypatch.setattr(providers, "_embed_query_or_none", fake_embed)

    with override_settings(EMBEDDINGS_BASE_URL="http://gateway-a.example/v1"):
        providers._embed_query_cached("pneumonia", "test")
    with override_settings(EMBEDDINGS_BASE_URL="http://gateway-b.example/v1"):
        providers._embed_query_cached("pneumonia", "test")

    assert len(calls) == 2


def test_a_repeated_semantic_search_embeds_once(group, report, settings):
    patcher, embed_query = _mock_embedding_client(settings.EMBEDDINGS_DIM)
    request = SemanticSearch(text="free air", filters=SearchFilters(group=group.pk, language="en"))
    try:
        first = search(request)
        second = search(request)
    finally:
        patcher.stop()

    assert embed_query.call_count == 1
    assert [d.document_id for d in first.documents] == [report.document_id]
    assert [d.document_id for d in second.documents] == [report.document_id]


def test_a_failed_semantic_embedding_is_not_cached(group, report, settings):
    patcher, embed_query = _mock_embedding_client(settings.EMBEDDINGS_DIM)
    embed_query.side_effect = [EmbeddingClientError("down"), _unit_vec(0, settings.EMBEDDINGS_DIM)]
    request = SemanticSearch(text="free air", filters=SearchFilters(group=group.pk, language="en"))
    try:
        first = search(request)
        second = search(request)
    finally:
        patcher.stop()

    assert first.degraded is True and first.documents == []
    assert second.degraded is False and len(second.documents) == 1
