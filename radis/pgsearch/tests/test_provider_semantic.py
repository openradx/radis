from unittest.mock import patch

import pytest
from django.contrib.auth.models import Group
from django.db import connection
from django.test.utils import CaptureQueriesContext

from radis.core.utils.embedding_client import EmbeddingClientError
from radis.core.utils.model_spec import parse_model_spec
from radis.pgsearch.models import ReportSearchIndex
from radis.pgsearch.providers import ITERATIVE_SCAN_SQL, search
from radis.reports.factories import ReportFactory
from radis.search.site import SearchFilters, SemanticSearch

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _embeddings_model_configured(settings):
    settings.EMBEDDINGS_MODEL = parse_model_spec("qwen3")


def _vec(dim: int, components: dict[int, float]) -> list[float]:
    v = [0.0] * dim
    for idx, value in components.items():
        v[idx] = value
    return v


def _request(
    group_id: int, text: str = "free air in the pleural space", **kwargs
) -> SemanticSearch:
    return SemanticSearch(text=text, filters=SearchFilters(group=group_id, language="en"), **kwargs)


@pytest.fixture
def group(db):
    return Group.objects.create(name="radiology")


@pytest.fixture
def corpus(group, settings):
    """Similarities to the mocked query vector (1, 0, ...): near 0.8, mid 0.6, far 0.0;
    ``unembedded`` has no vector and is invisible in semantic mode."""
    dim = settings.EMBEDDINGS_DIM
    near = ReportFactory.create(
        body="Lungs are clear bilaterally & no effusion.", language__code="en"
    )
    mid = ReportFactory.create(body="Findings: pneumothorax on the left.", language__code="en")
    far = ReportFactory.create(body="Degenerative changes of the spine.", language__code="en")
    unembedded = ReportFactory.create(body="Not embedded yet.", language__code="en")
    for report in (near, mid, far, unembedded):
        report.groups.add(group)
    ReportSearchIndex.objects.filter(report=near).update(embedding=_vec(dim, {0: 0.8, 1: 0.6}))
    ReportSearchIndex.objects.filter(report=mid).update(embedding=_vec(dim, {0: 0.6, 1: 0.8}))
    ReportSearchIndex.objects.filter(report=far).update(embedding=_vec(dim, {1: 1.0}))
    return {"near": near, "mid": mid, "far": far, "unembedded": unembedded}


@pytest.fixture
def embed(settings):
    patcher = patch("radis.pgsearch.providers.EmbeddingClient")
    client_cls = patcher.start()
    client_cls.return_value.__enter__.return_value = client_cls.return_value
    client_cls.return_value.__exit__.return_value = None
    client_cls.return_value.embed_query.return_value = _vec(settings.EMBEDDINGS_DIM, {0: 1.0})
    yield client_cls.return_value.embed_query
    patcher.stop()


def _ids(result) -> list[str]:
    return [d.document_id for d in result.documents]


def test_results_come_in_similarity_order(group, corpus, embed):
    result = search(_request(group.pk))

    assert _ids(result) == [
        corpus["near"].document_id,
        corpus["mid"].document_id,
        corpus["far"].document_id,
    ]
    assert result.total_count == 3
    assert result.total_relation == "exact"
    assert result.degraded is False
    near_doc = result.documents[0]
    assert near_doc.similarity == pytest.approx(0.8)
    assert near_doc.score == near_doc.similarity
    assert near_doc.relevance is None
    assert near_doc.summary == "Lungs are clear bilaterally &amp; no effusion."
    assert "<em>" not in near_doc.summary
    embed.assert_called_once_with("free air in the pleural space")


def test_filters_apply(corpus, embed, settings):
    other = Group.objects.create(name="cardiology")
    foreign = ReportFactory.create(body="Cardiomegaly.", language__code="en")
    foreign.groups.add(other)
    ReportSearchIndex.objects.filter(report=foreign).update(
        embedding=_vec(settings.EMBEDDINGS_DIM, {0: 1.0})
    )

    result = search(_request(other.pk))

    assert _ids(result) == [foreign.document_id]


def test_a_zero_vector_counts_as_not_embedded(group, corpus, embed, settings):
    """pgvector's cosine distance to a zero vector is NaN. The HNSW index leaves such a
    row out but every other plan returns it, so index scans are switched off for the
    rest of the test's transaction to make the database return it."""
    zero = ReportFactory.create(body="Zero vector.", language__code="en")
    zero.groups.add(group)
    ReportSearchIndex.objects.filter(report=zero).update(
        embedding=_vec(settings.EMBEDDINGS_DIM, {})
    )
    with connection.cursor() as cursor:
        cursor.execute("SET LOCAL enable_indexscan = off")

    result = search(_request(group.pk))
    with CaptureQueriesContext(connection) as ctx:
        cached = search(_request(group.pk))

    expected = [corpus["near"].document_id, corpus["mid"].document_id, corpus["far"].document_id]
    assert _ids(result) == _ids(cached) == expected
    assert result.total_count == cached.total_count == 3
    assert not any("<=>" in q["sql"] for q in ctx.captured_queries)


def test_the_setting_truncates_the_list(group, corpus, embed, settings):
    settings.SEARCH_SEMANTIC_MIN_SIMILARITY = 0.5

    result = search(_request(group.pk))

    assert _ids(result) == [corpus["near"].document_id, corpus["mid"].document_id]
    assert result.total_count == 2


def test_the_per_search_cutoff_beats_the_setting(group, corpus, embed, settings):
    settings.SEARCH_SEMANTIC_MIN_SIMILARITY = 0.5

    assert _ids(search(_request(group.pk, min_similarity=0.7))) == [corpus["near"].document_id]
    assert len(_ids(search(_request(group.pk, min_similarity=0.0)))) == 3


def test_a_cutoff_nothing_passes_gives_an_empty_result(group, corpus, embed):
    result = search(_request(group.pk, min_similarity=1.0))

    assert result.documents == []
    assert result.total_count == 0
    assert result.degraded is False


def test_truncation_happens_after_retrieval(group, corpus, embed):
    search(_request(group.pk, min_similarity=0.7))
    with CaptureQueriesContext(connection) as ctx:
        result = search(_request(group.pk))

    assert not any("<=>" in q["sql"] for q in ctx.captured_queries)
    assert result.total_count == 3


def test_iterative_scan_is_set_for_the_scan_only(group, corpus, embed, settings):
    settings.SEARCH_SEMANTIC_ITERATIVE_SCAN = True
    with CaptureQueriesContext(connection) as ctx:
        search(_request(group.pk))

    statements = [q["sql"] for q in ctx.captured_queries]
    scan_index = next(i for i, sql in enumerate(statements) if "<=>" in sql)
    assert statements[scan_index - 1] == ITERATIVE_SCAN_SQL


def test_iterative_scan_can_be_switched_off(group, corpus, embed, settings):
    settings.SEARCH_SEMANTIC_ITERATIVE_SCAN = False
    with CaptureQueriesContext(connection) as ctx:
        search(_request(group.pk))

    assert not any("hnsw.iterative_scan" in q["sql"] for q in ctx.captured_queries)


def test_the_list_is_capped_at_the_beam_width(group, corpus, embed, settings):
    settings.SEARCH_HNSW_EF_SEARCH = 2
    with CaptureQueriesContext(connection) as ctx:
        result = search(_request(group.pk))

    assert result.total_count == 2
    assert any("LIMIT 2" in q["sql"] and "<=>" in q["sql"] for q in ctx.captured_queries)


def test_a_repeated_search_is_served_from_the_cache(group, corpus, embed):
    search(_request(group.pk))
    with CaptureQueriesContext(connection) as ctx:
        result = search(_request(group.pk))

    assert not any("<=>" in q["sql"] for q in ctx.captured_queries)
    assert embed.call_count == 1
    assert result.total_count == 3


def test_embedding_failure_is_empty_and_degraded_and_not_cached(group, corpus, embed, settings):
    embed.side_effect = [EmbeddingClientError("down"), _vec(settings.EMBEDDINGS_DIM, {0: 1.0})]

    first = search(_request(group.pk))
    second = search(_request(group.pk))

    assert first.documents == [] and first.total_count == 0 and first.degraded is True
    assert second.degraded is False and second.total_count == 3
    assert embed.call_count == 2


def test_a_semantic_search_without_a_model_is_a_caller_error(group, corpus, settings):
    settings.EMBEDDINGS_MODEL = None

    with pytest.raises(ValueError):
        search(_request(group.pk))
