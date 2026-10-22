from dataclasses import asdict
from unittest.mock import patch

import pytest
from django.contrib.auth.models import Group
from django.core.cache import cache
from django.db import connection
from django.test.utils import CaptureQueriesContext

from radis.core.utils.embedding_client import EmbeddingClientError
from radis.core.utils.model_spec import parse_model_spec
from radis.pgsearch.models import ReportSearchIndex
from radis.pgsearch.providers import _cache_key, count, search
from radis.reports.factories import ReportFactory
from radis.search.site import FullTextSearch, SearchFilters, SimilarityRerank
from radis.search.utils.query_parser import QueryParser

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _embeddings_model_configured(settings):
    settings.EMBEDDINGS_MODEL = parse_model_spec("qwen3")


def _unit_vec(idx: int, dim: int) -> list[float]:
    v = [0.0] * dim
    v[idx % dim] = 1.0
    return v


def _request(
    group_id: int,
    description: str | None = None,
    offset: int = 0,
    limit: int = 25,
    query: str = "pneumothorax",
):
    node, _ = QueryParser().parse(query)
    assert node is not None
    return FullTextSearch(
        query=node,
        filters=SearchFilters(group=group_id, language="en"),
        rerank=SimilarityRerank(description) if description else None,
        offset=offset,
        limit=limit,
    )


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


def _ran_measurement(queries) -> bool:
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


def test_a_described_search_shares_the_keyword_layer_with_a_plain_one(group, reports, embed):
    search(_request(group.pk))
    with CaptureQueriesContext(connection) as ctx:
        search(_request(group.pk, "free air"))

    assert not _ran_keyword_ranking(ctx.captured_queries)
    assert _ran_measurement(ctx.captured_queries)


def test_a_repeated_described_search_runs_neither_layer(group, reports, embed):
    search(_request(group.pk, "free air"))
    with CaptureQueriesContext(connection) as ctx:
        result = search(_request(group.pk, "free air"))

    assert not _ran_keyword_ranking(ctx.captured_queries)
    assert not _ran_measurement(ctx.captured_queries)
    assert embed.call_count == 1
    assert result.documents[0].similarity == pytest.approx(1.0)


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


def test_the_description_and_the_weight_participate_in_the_window_key(
    group, reports, embed, settings
):
    search(_request(group.pk, "free air"))
    with CaptureQueriesContext(connection) as other_text:
        search(_request(group.pk, "collapsed lung"))
    settings.SEARCH_RERANK_SEMANTIC_WEIGHT = 0.5
    with CaptureQueriesContext(connection) as other_weight:
        search(_request(group.pk, "collapsed lung"))

    assert _ran_measurement(other_text.captured_queries)
    assert _ran_measurement(other_weight.captured_queries)


def test_a_window_is_measured_again_when_the_keyword_list_changes(group, reports, embed, settings):
    request = _request(group.pk, "free air")
    search(request)
    joined = ReportFactory.create(body="Pneumothorax on the right.", language__code="en")
    joined.groups.add(group)
    ReportSearchIndex.objects.filter(report=joined).update(
        embedding=_unit_vec(0, settings.EMBEDDINGS_DIM)
    )
    # The keyword entry expires first and is recomputed with the new hit while the
    # window entry is still live.
    assert cache.delete(
        _cache_key(
            layer="keyword",
            query=QueryParser.unparse(request.query),
            filters=asdict(request.filters),
            fts_max=settings.SEARCH_FTS_MAX_RESULTS,
        )
    )

    with CaptureQueriesContext(connection) as ctx:
        result = search(request)

    assert _ran_measurement(ctx.captured_queries)
    by_id = {d.document_id: d for d in result.documents}
    assert by_id[joined.document_id].similarity == pytest.approx(1.0)


def test_a_degraded_window_is_not_cached_but_the_keyword_layer_is(group, reports, embed):
    embed.side_effect = EmbeddingClientError("down")
    degraded = search(_request(group.pk, "free air"))
    assert degraded.degraded is True

    embed.side_effect = None
    with CaptureQueriesContext(connection) as ctx:
        recovered = search(_request(group.pk, "free air"))

    assert not _ran_keyword_ranking(ctx.captured_queries)
    assert _ran_measurement(ctx.captured_queries)
    assert recovered.degraded is False
    assert recovered.documents[0].similarity == pytest.approx(1.0)


def test_a_described_search_without_keyword_hits_embeds_nothing(group, reports, embed):
    embed.side_effect = AssertionError("must not embed")

    result = search(_request(group.pk, "free air", query="pneumoperitoneum"))

    assert result.total_count == 0
    assert result.degraded is False
    embed.assert_not_called()


def test_zero_timeout_disables_the_cache(group, reports, settings):
    settings.SEARCH_RANKING_CACHE_TIMEOUT_SECONDS = 0
    search(_request(group.pk))
    with CaptureQueriesContext(connection) as ctx:
        search(_request(group.pk))

    assert _ran_keyword_ranking(ctx.captured_queries)
