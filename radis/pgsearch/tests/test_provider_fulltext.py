from unittest.mock import patch

import pytest
from django.contrib.auth.models import Group
from django.db import connection
from django.test.utils import CaptureQueriesContext

from radis.core.utils.model_spec import parse_model_spec
from radis.pgsearch.providers import count, retrieve, search
from radis.reports.factories import ReportFactory
from radis.search.site import FullTextSearch, SearchFilters
from radis.search.utils.query_parser import QueryParser

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _search_settings(settings):
    """Test settings default EMBEDDINGS_MODEL to None; a configured model shows that
    full-text mode does not embed even when it could. The cache lifetime the
    assertions assume is pinned because a developer's .env reaches test runs outside
    Docker."""
    settings.EMBEDDINGS_MODEL = parse_model_spec("qwen3")
    settings.SEARCH_RANKING_CACHE_TIMEOUT_SECONDS = 300


def _unit_vec(idx: int, dim: int) -> list[float]:
    v = [0.0] * dim
    v[idx % dim] = 1.0
    return v


def _request(query: str, group_id: int, **kwargs) -> FullTextSearch:
    node, _ = QueryParser().parse(query)
    assert node is not None
    return FullTextSearch(
        query=node,
        # language="en" so the query-side tsquery config matches the reports' config.
        filters=SearchFilters(group=group_id, language="en"),
        **kwargs,
    )


@pytest.fixture
def group(db):
    return Group.objects.create(name="radiology")


@pytest.fixture
def corpus(group):
    """Three keyword hits and one report the keywords never match.

    ``twice`` mentions the keyword twice, so it has the highest ts_rank; ``near``
    and ``bare`` tie on ts_rank and fall back to id order (near was created first).
    """
    twice = ReportFactory.create(
        body="No pneumothorax detected. Previous pneumothorax resolved.", language__code="en"
    )
    near = ReportFactory.create(body="Findings: pneumothorax on the left.", language__code="en")
    bare = ReportFactory.create(body="pneumothorax findings", language__code="en")
    other = ReportFactory.create(body="Lungs are clear bilaterally.", language__code="en")
    for report in (twice, near, bare, other):
        report.groups.add(group)
    return {"twice": twice, "near": near, "bare": bare, "other": other}


def _mock_client(dim: int, idx: int = 0):
    patcher = patch("radis.pgsearch.providers.EmbeddingClient")
    client_cls = patcher.start()
    client_cls.return_value.__enter__.return_value = client_cls.return_value
    client_cls.return_value.__exit__.return_value = None
    client_cls.return_value.embed_query.return_value = _unit_vec(idx, dim)
    return patcher, client_cls


@pytest.fixture
def embed(settings):
    patcher, client_cls = _mock_client(settings.EMBEDDINGS_DIM)
    yield client_cls
    patcher.stop()


def _ids(result) -> list[str]:
    return [d.document_id for d in result.documents]


def test_keyword_search_makes_no_embedding_call_and_keeps_keyword_order(group, corpus, embed):
    result = search(_request("pneumothorax", group.pk))

    assert embed.return_value.embed_query.call_count == 0
    assert _ids(result) == [
        corpus["twice"].document_id,
        corpus["near"].document_id,
        corpus["bare"].document_id,
    ]
    assert result.total_count == 3
    assert result.total_relation == "exact"
    assert result.degraded is False
    for doc in result.documents:
        assert doc.similarity is None
        assert doc.score == doc.relevance


def test_count_and_retrieve_return_the_keyword_hits_without_embedding(group, corpus, embed):
    embed.return_value.embed_query.side_effect = AssertionError("must not embed")
    request = _request("pneumothorax", group.pk)

    assert count(request) == 3
    assert list(retrieve(request)) == [
        corpus["twice"].document_id,
        corpus["near"].document_id,
        corpus["bare"].document_id,
    ]


def test_hydration_skips_a_report_removed_from_the_group(group, corpus, embed):
    request = _request("pneumothorax", group.pk)
    search(request)  # caches the keyword ranking
    corpus["near"].groups.remove(group)

    result = search(request)

    assert corpus["near"].document_id not in _ids(result)
    assert result.total_count == 3  # the cached count catches up when the entry expires


def test_retrieve_skips_a_report_removed_from_the_group(group, corpus):
    request = _request("pneumothorax", group.pk)
    list(retrieve(request))  # caches the keyword ranking
    corpus["near"].groups.remove(group)

    assert list(retrieve(request)) == [corpus["twice"].document_id, corpus["bare"].document_id]
    assert count(request) == 3  # the cached count catches up when the entry expires


@pytest.mark.parametrize("limit", [0, -1])
def test_a_non_positive_limit_hydrates_nothing(group, corpus, limit):
    request = _request("pneumothorax", group.pk, limit=limit)
    search(request)  # caches the keyword ranking

    with CaptureQueriesContext(connection) as ctx:
        result = search(request)

    assert result.documents == []
    assert result.total_count == 3
    # With the ranking cached, the hydration would be the only query.
    assert ctx.captured_queries == []


def test_a_cached_page_hydrates_in_two_queries(group):
    modalities = [["CT"], ["MR"], ["CT", "PT"], [], ["US"]]
    reports = [
        ReportFactory.create(body="pneumothorax", language__code="en", modalities=codes)
        for codes in modalities
    ]
    for report in reports:
        report.groups.add(group)
    request = _request("pneumothorax", group.pk)
    search(request)  # caches the keyword ranking

    with CaptureQueriesContext(connection) as ctx:
        result = search(request)

    # One query reads the page's index rows with their reports and headlines, one more
    # reads the modalities of all five reports; the ranking comes from the cache.
    assert len(ctx.captured_queries) == 2
    assert {d.document_id: sorted(d.modalities) for d in result.documents} == {
        report.document_id: sorted(codes) for report, codes in zip(reports, modalities)
    }
