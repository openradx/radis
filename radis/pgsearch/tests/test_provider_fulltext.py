import logging
from unittest.mock import patch

import pytest
from django.contrib.auth.models import Group
from django.db import connection
from django.test.utils import CaptureQueriesContext

from radis.core.utils.embedding_client import EmbeddingClientError
from radis.core.utils.model_spec import parse_model_spec
from radis.core.utils.rate_limit import RateLimited
from radis.pgsearch.models import ReportSearchIndex
from radis.pgsearch.providers import count, retrieve, search
from radis.reports.factories import ReportFactory
from radis.search.site import FullTextSearch, SearchFilters, SimilarityRerank
from radis.search.utils.query_parser import QueryParser

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _search_settings(settings):
    """Test settings default EMBEDDINGS_MODEL to None; the re-rank needs a model and a
    mocked client. The assertions assume the default re-rank weight, window and cache
    lifetime, pinned because a developer's .env reaches test runs outside Docker."""
    settings.EMBEDDINGS_MODEL = parse_model_spec("qwen3")
    settings.SEARCH_RERANK_SEMANTIC_WEIGHT = 0.7
    settings.SEARCH_RERANK_WINDOW = 1000
    settings.SEARCH_RANKING_CACHE_TIMEOUT_SECONDS = 300


def _unit_vec(idx: int, dim: int) -> list[float]:
    v = [0.0] * dim
    v[idx % dim] = 1.0
    return v


def _request(query: str, group_id: int, description: str | None = None, **kwargs) -> FullTextSearch:
    node, _ = QueryParser().parse(query)
    assert node is not None
    return FullTextSearch(
        query=node,
        # language="en" so the query-side tsquery config matches the reports' config.
        filters=SearchFilters(group=group_id, language="en"),
        rerank=SimilarityRerank(description) if description else None,
        **kwargs,
    )


@pytest.fixture
def group(db):
    return Group.objects.create(name="radiology")


@pytest.fixture
def corpus(group, settings):
    """Three keyword hits and one report the keywords never match.

    ``twice`` mentions the keyword twice, so it has the highest ts_rank; ``near``
    and ``bare`` tie on ts_rank and fall back to id order (near was created first).
    """
    dim = settings.EMBEDDINGS_DIM
    twice = ReportFactory.create(
        body="No pneumothorax detected. Previous pneumothorax resolved.", language__code="en"
    )
    near = ReportFactory.create(body="Findings: pneumothorax on the left.", language__code="en")
    bare = ReportFactory.create(body="pneumothorax findings", language__code="en")
    other = ReportFactory.create(body="Lungs are clear bilaterally.", language__code="en")
    for report in (twice, near, bare, other):
        report.groups.add(group)
    ReportSearchIndex.objects.filter(report=twice).update(embedding=_unit_vec(99, dim))
    ReportSearchIndex.objects.filter(report=near).update(embedding=_unit_vec(0, dim))
    ReportSearchIndex.objects.filter(report=other).update(embedding=_unit_vec(0, dim))
    # ``bare`` keeps a NULL embedding: not embedded yet.
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


def test_a_description_reranks_the_window(group, corpus, embed):
    result = search(_request("pneumothorax", group.pk, "free air in the pleural space"))

    # near is identical to the description vector (similarity 1), twice is orthogonal
    # (0); at w=0.7 near scores 0.7 and twice 0.3. bare has no vector and follows.
    assert _ids(result) == [
        corpus["near"].document_id,
        corpus["twice"].document_id,
        corpus["bare"].document_id,
    ]
    by_id = {d.document_id: d for d in result.documents}
    assert by_id[corpus["near"].document_id].similarity == pytest.approx(1.0)
    assert by_id[corpus["near"].document_id].score == pytest.approx(0.7)
    assert by_id[corpus["twice"].document_id].similarity == pytest.approx(0.0)
    assert by_id[corpus["twice"].document_id].score == pytest.approx(0.3)
    assert by_id[corpus["bare"].document_id].similarity is None
    assert by_id[corpus["bare"].document_id].score == by_id[corpus["bare"].document_id].relevance
    assert corpus["other"].document_id not in by_id
    assert result.degraded is False


def test_the_rerank_embeds_the_description_not_the_keywords(group, corpus, embed):
    search(_request("pneumothorax", group.pk, "free air in the pleural space"))

    embed.return_value.embed_query.assert_called_once_with("free air in the pleural space")


def test_only_the_window_is_measured(group, corpus, embed, settings):
    settings.SEARCH_RERANK_WINDOW = 1
    result = search(_request("pneumothorax", group.pk, "free air"))

    # Only ``twice`` (the top keyword hit) is measured; the others keep keyword order.
    assert _ids(result) == [
        corpus["twice"].document_id,
        corpus["near"].document_id,
        corpus["bare"].document_id,
    ]
    assert result.documents[1].similarity is None


def test_a_zero_vector_counts_as_not_embedded(group, corpus, embed, settings):
    dim = settings.EMBEDDINGS_DIM
    # pgvector's cosine distance to a zero vector is NaN.
    ReportSearchIndex.objects.filter(report=corpus["twice"]).update(embedding=[0.0] * dim)
    ReportSearchIndex.objects.filter(report=corpus["bare"]).update(embedding=_unit_vec(5, dim))

    result = search(_request("pneumothorax", group.pk, "free air"))

    # ``twice`` is the top keyword hit, yet it follows both measured hits: the tail.
    assert _ids(result) == [
        corpus["near"].document_id,
        corpus["bare"].document_id,
        corpus["twice"].document_id,
    ]
    zero = result.documents[2]
    assert zero.similarity is None
    assert zero.score == zero.relevance


def test_the_measurement_is_one_id_lookup(group, corpus, embed):
    with CaptureQueriesContext(connection) as ctx:
        search(_request("pneumothorax", group.pk, "free air"))

    measurements = [q["sql"] for q in ctx.captured_queries if "<=>" in q["sql"]]
    assert len(measurements) == 1
    assert '"report_id" IN' in measurements[0]


@pytest.mark.parametrize(
    "failure",
    [EmbeddingClientError("down"), RateLimited()],
    ids=["client-error", "rate-limited"],
)
def test_embedding_failure_keeps_keyword_order_and_flags_degraded(group, corpus, embed, failure):
    embed.return_value.embed_query.side_effect = failure

    result = search(_request("pneumothorax", group.pk, "free air"))

    assert _ids(result) == [
        corpus["twice"].document_id,
        corpus["near"].document_id,
        corpus["bare"].document_id,
    ]
    assert result.degraded is True
    assert all(d.similarity is None for d in result.documents)


def test_openai_rate_limit_error_flags_degraded(group, corpus, embed):
    import httpx2
    import openai

    response = httpx2.Response(429, request=httpx2.Request("POST", "http://x"))
    embed.return_value.embed_query.side_effect = openai.RateLimitError(
        message="slow down", response=response, body=None
    )

    result = search(_request("pneumothorax", group.pk, "free air"))

    assert result.degraded is True
    assert result.total_count == 3


def test_a_rerank_without_a_model_embeds_nothing(group, corpus, settings, monkeypatch, caplog):
    from radis.pgsearch import providers

    settings.EMBEDDINGS_MODEL = None
    calls = []
    monkeypatch.setattr(providers, "_embed_query_cached", lambda text, caller: calls.append(text))

    with caplog.at_level(logging.ERROR, logger="radis.pgsearch.providers"):
        result = search(_request("pneumothorax", group.pk, "free air"))

    assert calls == []
    assert caplog.records == []
    assert result.degraded is False
    assert result.total_count == 3


def test_the_query_path_timeout_reaches_the_client(group, corpus, embed, settings):
    settings.EMBEDDINGS_QUERY_REQUEST_TIMEOUT_SECONDS = 3.5

    search(_request("pneumothorax", group.pk, "free air"))

    embed.assert_called_once_with(timeout=3.5)


def test_count_and_retrieve_ignore_the_rerank(group, corpus, embed):
    embed.return_value.embed_query.side_effect = AssertionError("must not embed")
    request = _request("pneumothorax", group.pk, "free air")

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
