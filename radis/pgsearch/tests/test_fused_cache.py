from unittest.mock import patch

import pytest
from django.contrib.auth.models import Group
from django.core.cache import cache
from django.db import connection
from django.test.utils import CaptureQueriesContext

from radis.core.utils.embedding_client import EmbeddingClientError
from radis.core.utils.model_spec import parse_model_spec
from radis.pgsearch.models import ReportSearchIndex
from radis.pgsearch.providers import search
from radis.reports.factories import ReportFactory
from radis.reports.models import Report
from radis.search.site import Search, SearchFilters
from radis.search.utils.query_parser import QueryParser

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _clear_cache():
    """The fused cache and the query-embedding cache share the process-local test
    cache backend, which outlives individual tests."""
    cache.clear()
    yield
    cache.clear()


@pytest.fixture(autouse=True)
def _embeddings_model_configured(settings):
    settings.EMBEDDINGS_MODEL = parse_model_spec("qwen3")


def _unit_vec(idx: int, dim: int) -> list[float]:
    v = [0.0] * dim
    v[idx % dim] = 1.0
    return v


def _make_report(body: str, **overrides) -> Report:
    """Pin language to ``en`` so index-side and query-side tsquery configs match
    (see test_provider_hybrid._make_report)."""
    return ReportFactory.create(body=body, language__code="en", **overrides)


def _make_search(query_str: str, group_id: int, offset: int = 0, limit: int = 25) -> Search:
    node, _ = QueryParser().parse(query_str)
    assert node is not None
    return Search(
        query=node,
        filters=SearchFilters(group=group_id, language="en"),
        offset=offset,
        limit=limit,
    )


@pytest.fixture
def group(db):
    return Group.objects.create(name="radiology")


@pytest.fixture
def reports(group, settings):
    dim = settings.EMBEDDINGS_DIM
    r_fts = _make_report(body="Findings: pneumothorax on the left.")
    r_fts.groups.add(group)
    # Doesn't lexically match "pneumothorax": reachable via the vector side only.
    r_vec = _make_report(body="Lungs are clear bilaterally.")
    r_vec.groups.add(group)
    ReportSearchIndex.objects.filter(report=r_fts).update(embedding=_unit_vec(99, dim))
    ReportSearchIndex.objects.filter(report=r_vec).update(embedding=_unit_vec(0, dim))
    return r_fts, r_vec


def _mock_embedding(dim: int, idx: int = 0):
    mocked = patch("radis.pgsearch.providers.EmbeddingClient")
    MockClient = mocked.start()
    MockClient.return_value.__enter__.return_value = MockClient.return_value
    MockClient.return_value.__exit__.return_value = None
    MockClient.return_value.embed_query.return_value = _unit_vec(idx, dim)
    return mocked, MockClient


def _ran_fusion(queries) -> bool:
    """Whether any captured query ran a fusion retriever.

    The FTS half ranks every match (ts_rank with no report_id list); the vector
    half is the only user of the <=> operator. The page-document fetch also
    annotates ts_rank/ts_headline, but only for an explicit ``report_id IN``
    page slice, and must not count as fusion work."""
    for q in queries:
        sql = q["sql"]
        if "<=>" in sql:
            return True
        if "ts_rank" in sql and '"report_id" IN' not in sql:
            return True
    return False


def test_second_identical_search_reuses_cached_fusion(group, reports, settings):
    mocked, _ = _mock_embedding(settings.EMBEDDINGS_DIM)
    try:
        with CaptureQueriesContext(connection) as first:
            result1 = search(_make_search("pneumothorax", group.pk))
        with CaptureQueriesContext(connection) as second:
            result2 = search(_make_search("pneumothorax", group.pk))
    finally:
        mocked.stop()

    assert _ran_fusion(first.captured_queries)
    # The fusion (FTS ranking + vector scan) must be served from cache; only the
    # page-document fetch may hit the database.
    assert not _ran_fusion(second.captured_queries)
    assert [d.document_id for d in result1.documents] == [
        d.document_id for d in result2.documents
    ]
    assert result1.total_count == result2.total_count


def test_pagination_is_served_from_the_cached_union(group, reports, settings):
    mocked, _ = _mock_embedding(settings.EMBEDDINGS_DIM)
    try:
        page1 = search(_make_search("pneumothorax", group.pk, offset=0, limit=1))
        with CaptureQueriesContext(connection) as ctx:
            page2 = search(_make_search("pneumothorax", group.pk, offset=1, limit=1))
    finally:
        mocked.stop()

    assert not _ran_fusion(ctx.captured_queries)
    ids1 = [d.document_id for d in page1.documents]
    ids2 = [d.document_id for d in page2.documents]
    assert len(ids1) == len(ids2) == 1
    assert ids1 != ids2  # genuinely the next page of the same union


def test_filters_participate_in_the_cache_key(reports, settings):
    other = Group.objects.create(name="cardiology")
    r_other = _make_report(body="No pneumothorax after drainage.")
    r_other.groups.add(other)
    ReportSearchIndex.objects.filter(report=r_other).update(
        embedding=_unit_vec(1, settings.EMBEDDINGS_DIM)
    )
    r_fts, _ = reports

    mocked, _ = _mock_embedding(settings.EMBEDDINGS_DIM)
    try:
        first_group = reports[0].groups.first()
        result_first = search(_make_search("pneumothorax", first_group.pk))
        result_other = search(_make_search("pneumothorax", other.pk))
    finally:
        mocked.stop()

    ids_first = {d.document_id for d in result_first.documents}
    ids_other = {d.document_id for d in result_other.documents}
    assert r_fts.document_id in ids_first
    assert ids_other == {r_other.document_id}


def test_degraded_fts_only_result_is_not_cached(group, reports, settings):
    r_fts, r_vec = reports
    mocked, MockClient = _mock_embedding(settings.EMBEDDINGS_DIM)
    try:
        MockClient.return_value.embed_query.side_effect = EmbeddingClientError("down")
        degraded = search(_make_search("pneumothorax", group.pk))
        assert {d.document_id for d in degraded.documents} == {r_fts.document_id}

        # Service recovers: the next search must not be pinned to the degraded union.
        MockClient.return_value.embed_query.side_effect = None
        recovered = search(_make_search("pneumothorax", group.pk))
    finally:
        mocked.stop()

    assert r_vec.document_id in {d.document_id for d in recovered.documents}


def test_zero_timeout_disables_the_cache(group, reports, settings):
    settings.HYBRID_FUSED_CACHE_TIMEOUT_SECONDS = 0
    mocked, _ = _mock_embedding(settings.EMBEDDINGS_DIM)
    try:
        search(_make_search("pneumothorax", group.pk))
        with CaptureQueriesContext(connection) as ctx:
            search(_make_search("pneumothorax", group.pk))
    finally:
        mocked.stop()

    assert _ran_fusion(ctx.captured_queries)


def test_fusion_timings_are_logged(group, reports, settings, caplog):
    import logging

    mocked, _ = _mock_embedding(settings.EMBEDDINGS_DIM)
    try:
        with caplog.at_level(logging.INFO, logger="radis.pgsearch.providers"):
            search(_make_search("pneumothorax", group.pk))
            search(_make_search("pneumothorax", group.pk))
    finally:
        mocked.stop()

    timing_lines = [r.message for r in caplog.records if "hybrid fusion timings" in r.message]
    hit_lines = [r.message for r in caplog.records if "hybrid fusion cache hit" in r.message]
    assert len(timing_lines) == 1  # only the uncached run measures the arms
    assert len(hit_lines) == 1
    line = timing_lines[0]
    for key in ("fts_ms=", "fts_rows=", "embed_ms=", "vec_ms=", "fuse_ms=", "total_ms="):
        assert key in line
    # The query is logged as a hash, never as the search text.
    assert "pneumothorax" not in line
