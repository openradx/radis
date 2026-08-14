from unittest.mock import patch

import pytest
from django.contrib.auth.models import Group

from radis.pgsearch.models import ReportSearchIndex
from radis.pgsearch.providers import retrieve, search
from radis.pgsearch.utils.embedding_client import EmbeddingClientError
from radis.reports.factories import ReportFactory
from radis.search.site import Search, SearchFilters
from radis.search.utils.query_parser import QueryParser

pytestmark = pytest.mark.django_db


def _unit_vec(idx: int, dim: int) -> list[float]:
    v = [0.0] * dim
    v[idx % dim] = 1.0
    return v


def _make_search(query_str: str, group_id: int, offset: int = 0) -> Search:
    node, _ = QueryParser().parse(query_str)
    assert node is not None
    return Search(
        query=node,
        filters=SearchFilters(group=group_id),
        offset=offset,
        limit=25,
    )


@pytest.fixture
def group(db):
    return Group.objects.create(name="radiology")


@pytest.fixture
def vector_only_report(group, settings):
    """A report only reachable through the vector side, so results prove the
    query embedding was actually used (not just that no exception occurred)."""
    r = ReportFactory.create(body="Lungs are clear bilaterally.")
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


def test_paginated_search_embeds_query_only_once(group, vector_only_report, settings):
    patcher, embed_query = _mock_embedding_client(settings.EMBEDDINGS_DIM)
    try:
        first = search(_make_search("pneumothorax", group.pk, offset=0))
        second = search(_make_search("pneumothorax", group.pk, offset=0))
    finally:
        patcher.stop()

    assert embed_query.call_count == 1
    # The cached vector was really used: the vector-only report shows up both times.
    for result in (first, second):
        assert vector_only_report.document_id in [d.document_id for d in result.documents]


def test_embedding_failure_is_not_cached(group, vector_only_report, settings):
    """A transient embedding outage must not pin later searches to FTS-only."""
    patcher, embed_query = _mock_embedding_client(settings.EMBEDDINGS_DIM)
    embed_query.side_effect = [
        EmbeddingClientError("down"),
        _unit_vec(0, settings.EMBEDDINGS_DIM),
    ]
    try:
        first = search(_make_search("pneumothorax", group.pk))
        second = search(_make_search("pneumothorax", group.pk))
    finally:
        patcher.stop()

    assert embed_query.call_count == 2
    assert vector_only_report.document_id not in [d.document_id for d in first.documents]
    assert vector_only_report.document_id in [d.document_id for d in second.documents]


def test_different_queries_embed_separately(group, settings):
    patcher, embed_query = _mock_embedding_client(settings.EMBEDDINGS_DIM)
    try:
        search(_make_search("pneumothorax", group.pk))
        search(_make_search("pleural effusion", group.pk))
    finally:
        patcher.stop()

    assert embed_query.call_count == 2


def test_retrieve_shares_cache_with_search(group, vector_only_report, settings):
    patcher, embed_query = _mock_embedding_client(settings.EMBEDDINGS_DIM)
    try:
        search(_make_search("pneumothorax", group.pk))
        doc_ids = list(retrieve(_make_search("pneumothorax", group.pk)))
    finally:
        patcher.stop()

    assert embed_query.call_count == 1
    assert vector_only_report.document_id in doc_ids
