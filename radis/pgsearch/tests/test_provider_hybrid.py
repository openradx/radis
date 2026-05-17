from unittest.mock import patch

import pytest
from django.contrib.auth.models import Group

from radis.pgsearch.models import ReportSearchVector
from radis.pgsearch.providers import retrieve, search
from radis.pgsearch.utils.embedding_client import EmbeddingClientError
from radis.reports.factories import ReportFactory
from radis.search.site import Search, SearchFilters
from radis.search.utils.query_parser import QueryParser

pytestmark = pytest.mark.django_db


def _unit_vec(idx: int, dim: int) -> list[float]:
    """Deterministic unit vector that points in dimension `idx`."""
    v = [0.0] * dim
    v[idx % dim] = 1.0
    return v


def _make_search(query_str: str, group_id: int) -> Search:
    node, _ = QueryParser().parse(query_str)
    assert node is not None
    return Search(
        query=node,
        filters=SearchFilters(group=group_id),
        offset=0,
        limit=25,
    )


@pytest.fixture
def group(db):
    return Group.objects.create(name="radiology")


@pytest.fixture
def reports_with_embeddings(group, settings):
    dim = settings.EMBEDDING_DIM
    # r0: matches FTS for "pneumothorax", vector unrelated (dim 99)
    r0 = ReportFactory.create(body="Findings: pneumothorax on the left.")
    r0.groups.add(group)
    # r1: doesn't lexically match "pneumothorax"; embedding at dim 1 (not identical to query dim 0)
    r1 = ReportFactory.create(body="Lungs are clear bilaterally.")
    r1.groups.add(group)
    # r2: matches FTS (multiple times for stronger ts_rank) AND vector exactly at query dim 0
    r2 = ReportFactory.create(
        body="No pneumothorax detected. Previous pneumothorax resolved. Lungs clear."
    )
    r2.groups.add(group)
    ReportSearchVector.objects.filter(report=r0).update(embedding=_unit_vec(99, dim))
    ReportSearchVector.objects.filter(report=r1).update(embedding=_unit_vec(1, dim))
    ReportSearchVector.objects.filter(report=r2).update(embedding=_unit_vec(0, dim))
    return r0, r1, r2


def test_hybrid_returns_fts_only_hit(group, reports_with_embeddings, settings):
    r0, _, _ = reports_with_embeddings
    dim = settings.EMBEDDING_DIM
    # Query vector points at dim 50 — far from all docs. So vec_top_K still
    # returns docs, but their distances are large. FTS for "pneumothorax"
    # picks up r0 and r2.
    with patch("radis.pgsearch.providers.EmbeddingClient") as MockClient:
        MockClient.return_value.__enter__.return_value = MockClient.return_value
        MockClient.return_value.__exit__.return_value = None
        MockClient.return_value.embed_query.return_value = _unit_vec(50, dim)
        result = search(_make_search("pneumothorax", group.pk))

    ids = [d.document_id for d in result.documents]
    assert r0.document_id in ids


def test_hybrid_returns_vector_only_hit(group, reports_with_embeddings, settings):
    _, r1, _ = reports_with_embeddings
    dim = settings.EMBEDDING_DIM
    # Query vector at dim 0 — closest to r1 and r2. FTS for "pneumothorax"
    # excludes r1 lexically; vector side must surface it.
    with patch("radis.pgsearch.providers.EmbeddingClient") as MockClient:
        MockClient.return_value.__enter__.return_value = MockClient.return_value
        MockClient.return_value.__exit__.return_value = None
        MockClient.return_value.embed_query.return_value = _unit_vec(0, dim)
        result = search(_make_search("pneumothorax", group.pk))

    ids = [d.document_id for d in result.documents]
    assert r1.document_id in ids


def test_hybrid_both_sides_match_ranks_first(group, reports_with_embeddings, settings):
    _, _, r2 = reports_with_embeddings
    dim = settings.EMBEDDING_DIM
    with patch("radis.pgsearch.providers.EmbeddingClient") as MockClient:
        MockClient.return_value.__enter__.return_value = MockClient.return_value
        MockClient.return_value.__exit__.return_value = None
        MockClient.return_value.embed_query.return_value = _unit_vec(0, dim)
        result = search(_make_search("pneumothorax", group.pk))

    ids = [d.document_id for d in result.documents]
    # r2 is in both vec_top_K and FTS hits; should rank above pure-side matches.
    assert ids[0] == r2.document_id


def test_embedding_failure_falls_back_to_fts(group, reports_with_embeddings):
    r0, _, r2 = reports_with_embeddings
    with patch("radis.pgsearch.providers.EmbeddingClient") as MockClient:
        MockClient.return_value.__enter__.return_value = MockClient.return_value
        MockClient.return_value.__exit__.return_value = None
        MockClient.return_value.embed_query.side_effect = EmbeddingClientError("down")
        result = search(_make_search("pneumothorax", group.pk))

    ids = [d.document_id for d in result.documents]
    # Both FTS-matching reports come back, no vector-only ones.
    assert set(ids) == {r0.document_id, r2.document_id}


def test_reports_with_null_embedding_still_returned_via_fts(group, settings):
    dim = settings.EMBEDDING_DIM
    r = ReportFactory.create(body="pneumothorax findings")
    r.groups.add(group)
    # Leave embedding NULL.
    with patch("radis.pgsearch.providers.EmbeddingClient") as MockClient:
        MockClient.return_value.__enter__.return_value = MockClient.return_value
        MockClient.return_value.__exit__.return_value = None
        MockClient.return_value.embed_query.return_value = _unit_vec(0, dim)
        result = search(_make_search("pneumothorax", group.pk))

    ids = [d.document_id for d in result.documents]
    assert r.document_id in ids


def test_empty_summary_falls_back_to_body_head(group, settings):
    dim = settings.EMBEDDING_DIM
    # Doc whose body does not contain the query word — vector-only hit.
    r = ReportFactory.create(
        body="lung parenchyma demonstrates clear bilaterally with no abnormality",
    )
    r.groups.add(group)
    ReportSearchVector.objects.filter(report=r).update(embedding=_unit_vec(0, dim))

    with patch("radis.pgsearch.providers.EmbeddingClient") as MockClient:
        MockClient.return_value.__enter__.return_value = MockClient.return_value
        MockClient.return_value.__exit__.return_value = None
        MockClient.return_value.embed_query.return_value = _unit_vec(0, dim)
        result = search(_make_search("pneumothorax", group.pk))

    doc = next(d for d in result.documents if d.document_id == r.document_id)
    # Summary is non-empty (fell back to body head) and is plain text (no <em>).
    assert doc.summary
    assert "<em>" not in doc.summary


def test_retrieve_returns_hybrid_ordered_document_ids(group, reports_with_embeddings, settings):
    r0, r1, r2 = reports_with_embeddings
    dim = settings.EMBEDDING_DIM
    with patch("radis.pgsearch.providers.EmbeddingClient") as MockClient:
        MockClient.return_value.__enter__.return_value = MockClient.return_value
        MockClient.return_value.__exit__.return_value = None
        MockClient.return_value.embed_query.return_value = _unit_vec(0, dim)
        doc_ids = list(retrieve(_make_search("pneumothorax", group.pk)))

    # r2 (both sides) first, then any order containing r0 and r1.
    assert doc_ids[0] == r2.document_id
    assert set(doc_ids) >= {r0.document_id, r1.document_id, r2.document_id}


def test_retrieve_falls_back_to_fts_on_embedding_error(group, reports_with_embeddings):
    r0, _, r2 = reports_with_embeddings
    with patch("radis.pgsearch.providers.EmbeddingClient") as MockClient:
        MockClient.return_value.__enter__.return_value = MockClient.return_value
        MockClient.return_value.__exit__.return_value = None
        MockClient.return_value.embed_query.side_effect = EmbeddingClientError("down")
        doc_ids = list(retrieve(_make_search("pneumothorax", group.pk)))
    assert set(doc_ids) == {r0.document_id, r2.document_id}


def test_documents_carry_cosine_distance_and_rrf_score(
    group, reports_with_embeddings, settings
):
    """Verify cosine_distance is set for vector-side hits and rrf_score reflects fusion."""
    _, _, r2 = reports_with_embeddings
    dim = settings.EMBEDDING_DIM
    with patch("radis.pgsearch.providers.EmbeddingClient") as MockClient:
        MockClient.return_value.__enter__.return_value = MockClient.return_value
        MockClient.return_value.__exit__.return_value = None
        MockClient.return_value.embed_query.return_value = _unit_vec(0, dim)
        result = search(_make_search("pneumothorax", group.pk))

    # r2 is in both vector top-K and FTS hits, so its rrf_score should be the largest.
    top = result.documents[0]
    assert top.document_id == r2.document_id
    assert top.cosine_distance is not None
    assert top.cosine_distance >= 0.0
    assert top.rrf_score > 0.0
    # All later documents have a strictly lower or equal rrf_score.
    for prev, curr in zip(result.documents, result.documents[1:]):
        assert curr.rrf_score <= prev.rrf_score
