from unittest.mock import patch

import pytest
from django.contrib.auth.models import Group
from django.db import connection
from django.test.utils import CaptureQueriesContext

from radis.core.utils.model_spec import parse_model_spec
from radis.pgsearch.models import ReportSearchIndex
from radis.pgsearch.providers import count, search
from radis.reports.factories import ReportFactory
from radis.search.site import FullTextSearch, SearchFilters, SemanticSearch
from radis.search.utils.query_parser import QueryParser

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _search_settings(settings):
    """A configured embedding model, and the cache lifetime the assertions assume,
    pinned because a developer's .env reaches test runs outside Docker."""
    settings.EMBEDDINGS_MODEL = parse_model_spec("qwen3")
    settings.SEARCH_RANKING_CACHE_TIMEOUT_SECONDS = 300


def _unit_vec(idx: int, dim: int) -> list[float]:
    v = [0.0] * dim
    v[idx % dim] = 1.0
    return v


def _request(group_id: int, offset: int = 0, limit: int = 25):
    node, _ = QueryParser().parse("pneumothorax")
    assert node is not None
    return FullTextSearch(
        query=node,
        filters=SearchFilters(group=group_id, language="en"),
        offset=offset,
        limit=limit,
    )


def _semantic_request(group_id: int, text: str = "free air") -> SemanticSearch:
    return SemanticSearch(text=text, filters=SearchFilters(group=group_id, language="en"))


@pytest.fixture
def group(db):
    return Group.objects.create(name="radiology")


@pytest.fixture
def reports(group, settings):
    dim = settings.EMBEDDINGS_DIM
    first = ReportFactory.create(body="Findings: pneumothorax on the left.", language__code="en")
    second = ReportFactory.create(
        body="No pneumothorax detected, lungs clear.", language__code="en"
    )
    for report in (first, second):
        report.groups.add(group)
    ReportSearchIndex.objects.filter(report=first).update(embedding=_unit_vec(0, dim))
    ReportSearchIndex.objects.filter(report=second).update(embedding=_unit_vec(1, dim))
    return first, second


@pytest.fixture
def embed(settings):
    patcher = patch("radis.pgsearch.providers.EmbeddingClient")
    client_cls = patcher.start()
    client_cls.return_value.__enter__.return_value = client_cls.return_value
    client_cls.return_value.__exit__.return_value = None
    client_cls.return_value.embed_query.return_value = _unit_vec(0, settings.EMBEDDINGS_DIM)
    yield client_cls.return_value.embed_query
    patcher.stop()


def _ran_keyword_ranking(queries) -> bool:
    """The keyword layer ranks every match: ts_rank with no report_id list. The
    hydration query also annotates ts_rank, but only for an explicit page slice."""
    return any("ts_rank" in q["sql"] and '"report_id" IN' not in q["sql"] for q in queries)


def _ran_vector_query(queries) -> bool:
    """The semantic scan is the only query that computes a cosine distance."""
    return any("<=>" in q["sql"] for q in queries)


def test_a_repeated_search_runs_no_ranking_query(group, reports):
    with CaptureQueriesContext(connection) as first:
        result1 = search(_request(group.pk))
    with CaptureQueriesContext(connection) as second:
        result2 = search(_request(group.pk))

    assert _ran_keyword_ranking(first.captured_queries)
    assert not _ran_keyword_ranking(second.captured_queries)
    assert [d.document_id for d in result1.documents] == [d.document_id for d in result2.documents]


def test_page_two_is_served_from_the_cached_ranking(group, reports):
    page1 = search(_request(group.pk, offset=0, limit=1))
    with CaptureQueriesContext(connection) as ctx:
        page2 = search(_request(group.pk, offset=1, limit=1))

    assert not _ran_keyword_ranking(ctx.captured_queries)
    assert len(page1.documents) == len(page2.documents) == 1
    assert page1.documents[0].document_id != page2.documents[0].document_id


def test_the_extraction_count_shares_the_keyword_layer(group, reports):
    search(_request(group.pk))
    with CaptureQueriesContext(connection) as ctx:
        assert count(_request(group.pk)) == 2

    assert not _ran_keyword_ranking(ctx.captured_queries)


def test_filters_participate_in_the_key(reports, settings):
    other = Group.objects.create(name="cardiology")
    r_other = ReportFactory.create(body="No pneumothorax after drainage.", language__code="en")
    r_other.groups.add(other)
    first, _ = reports

    result_first = search(_request(first.groups.first().pk))
    result_other = search(_request(other.pk))

    assert first.document_id in {d.document_id for d in result_first.documents}
    assert {d.document_id for d in result_other.documents} == {r_other.document_id}


def test_zero_timeout_disables_the_cache(group, reports, settings):
    settings.SEARCH_RANKING_CACHE_TIMEOUT_SECONDS = 0
    search(_request(group.pk))
    with CaptureQueriesContext(connection) as ctx:
        search(_request(group.pk))

    assert _ran_keyword_ranking(ctx.captured_queries)


def test_the_semantic_text_participates_in_the_key(group, reports, embed):
    search(_semantic_request(group.pk, "free air"))
    with CaptureQueriesContext(connection) as ctx:
        search(_semantic_request(group.pk, "collapsed lung"))

    assert _ran_vector_query(ctx.captured_queries)


def test_the_filters_participate_in_the_semantic_key(group, reports, embed, settings):
    other = Group.objects.create(name="cardiology")
    foreign = ReportFactory.create(body="Cardiomegaly.", language__code="en")
    foreign.groups.add(other)
    ReportSearchIndex.objects.filter(report=foreign).update(
        embedding=_unit_vec(0, settings.EMBEDDINGS_DIM)
    )
    search(_semantic_request(group.pk))
    with CaptureQueriesContext(connection) as ctx:
        result = search(_semantic_request(other.pk))

    assert _ran_vector_query(ctx.captured_queries)
    assert [d.document_id for d in result.documents] == [foreign.document_id]
    assert result.total_count == 1


def test_the_beam_width_participates_in_the_semantic_key(group, reports, embed, settings):
    search(_semantic_request(group.pk))
    settings.SEARCH_HNSW_EF_SEARCH = 1
    with CaptureQueriesContext(connection) as ctx:
        result = search(_semantic_request(group.pk))

    assert _ran_vector_query(ctx.captured_queries)
    assert result.total_count == 1


def test_the_iterative_scan_participates_in_the_semantic_key(group, reports, embed, settings):
    settings.SEARCH_SEMANTIC_ITERATIVE_SCAN = True
    search(_semantic_request(group.pk))
    settings.SEARCH_SEMANTIC_ITERATIVE_SCAN = False
    with CaptureQueriesContext(connection) as ctx:
        search(_semantic_request(group.pk))

    assert _ran_vector_query(ctx.captured_queries)


def test_the_embedding_configuration_participates_in_the_semantic_key(
    group, reports, embed, settings
):
    search(_semantic_request(group.pk))
    settings.EMBEDDINGS_BASE_URL = "http://other-gateway.example/v1"
    with CaptureQueriesContext(connection) as ctx:
        search(_semantic_request(group.pk))

    assert _ran_vector_query(ctx.captured_queries)


def test_zero_timeout_disables_the_semantic_cache(group, reports, embed, settings):
    settings.SEARCH_RANKING_CACHE_TIMEOUT_SECONDS = 0
    search(_semantic_request(group.pk))
    with CaptureQueriesContext(connection) as ctx:
        search(_semantic_request(group.pk))

    assert _ran_vector_query(ctx.captured_queries)
