import logging
from unittest.mock import patch

import pytest
from django.contrib.auth.models import Group
from django.test import override_settings

from radis.core.utils.embedding_client import EmbeddingClientError
from radis.core.utils.model_spec import parse_model_spec
from radis.pgsearch.models import ReportSearchIndex
from radis.pgsearch.providers import count, retrieve, search
from radis.reports.factories import ReportFactory
from radis.reports.models import Report
from radis.search.site import Search, SearchFilters
from radis.search.utils.query_parser import QueryParser

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _embeddings_model_configured(settings):
    """These tests exercise the vector side of hybrid search with EmbeddingClient
    mocked out; test settings default EMBEDDINGS_MODEL to None (FTS-only), which
    would make _embed_query_cached short-circuit before the mock is ever reached."""
    settings.EMBEDDINGS_MODEL = parse_model_spec("qwen3")


def _unit_vec(idx: int, dim: int) -> list[float]:
    """Deterministic unit vector that points in dimension `idx`."""
    v = [0.0] * dim
    v[idx % dim] = 1.0
    return v


def _make_report(body: str, **overrides) -> Report:
    """Create a report pinned to language ``en`` (like test_providers.py does).

    ReportFactory otherwise assigns a random Faker language code, and the index
    stems ``body`` with the config resolved from that language while the query
    side uses the config resolved from the search filters. A random language
    whose stemmer rewrites a query term (e.g. english: effusion -> effus) makes
    lexical matching — including negation exclusions — silently miss, turning
    these tests flaky."""
    return ReportFactory.create(body=body, language__code="en", **overrides)


def _make_search(query_str: str, group_id: int) -> Search:
    node, _ = QueryParser().parse(query_str)
    assert node is not None
    return Search(
        query=node,
        # language="en" so the query-side tsquery config matches the config the
        # reports from _make_report were indexed with (see its docstring).
        filters=SearchFilters(group=group_id, language="en"),
        offset=0,
        limit=25,
    )


@pytest.fixture
def group(db):
    return Group.objects.create(name="radiology")


@pytest.fixture
def reports_with_embeddings(group, settings):
    dim = settings.EMBEDDINGS_DIM
    # r0: matches FTS for "pneumothorax", vector unrelated (dim 99)
    r0 = _make_report(body="Findings: pneumothorax on the left.")
    r0.groups.add(group)
    # r1: doesn't lexically match "pneumothorax"; embedding at dim 1 (not identical to query dim 0)
    r1 = _make_report(body="Lungs are clear bilaterally.")
    r1.groups.add(group)
    # r2: matches FTS (multiple times for stronger ts_rank) AND vector exactly at query dim 0
    r2 = _make_report(body="No pneumothorax detected. Previous pneumothorax resolved. Lungs clear.")
    r2.groups.add(group)
    ReportSearchIndex.objects.filter(report=r0).update(embedding=_unit_vec(99, dim))
    ReportSearchIndex.objects.filter(report=r1).update(embedding=_unit_vec(1, dim))
    ReportSearchIndex.objects.filter(report=r2).update(embedding=_unit_vec(0, dim))
    return r0, r1, r2


def test_hybrid_returns_fts_only_hit(group, reports_with_embeddings, settings):
    r0, _, _ = reports_with_embeddings
    dim = settings.EMBEDDINGS_DIM
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
    dim = settings.EMBEDDINGS_DIM
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
    dim = settings.EMBEDDINGS_DIM
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


@override_settings(EMBEDDINGS_MODEL=None)
def test_search_without_a_configured_model_makes_no_embedding_call(
    group, reports_with_embeddings, caplog, monkeypatch
):
    """An FTS-only deployment must not pay for, or log, a failed embedding attempt.

    Constructing the client raises when no model is configured, and the search path
    catches that into logger.exception — a full traceback on every single query.
    """
    from radis.pgsearch import providers

    calls = []
    monkeypatch.setattr(providers, "_embed_query_cached", lambda text, caller: calls.append(text))

    with caplog.at_level(logging.ERROR, logger="radis.pgsearch.providers"):
        result = search(_make_search("pneumothorax", group.pk))

    assert calls == []
    assert caplog.records == []
    # FTS still works: r0 and r2 both mention pneumothorax.
    assert result.total_count == 2


def test_reports_with_null_embedding_still_returned_via_fts(group, settings):
    dim = settings.EMBEDDINGS_DIM
    r = _make_report(body="pneumothorax findings")
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
    dim = settings.EMBEDDINGS_DIM
    # Doc whose body does not contain the query word — vector-only hit.
    r = _make_report(
        body="lung parenchyma demonstrates clear bilaterally with no abnormality",
    )
    r.groups.add(group)
    ReportSearchIndex.objects.filter(report=r).update(embedding=_unit_vec(0, dim))

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
    dim = settings.EMBEDDINGS_DIM
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


def test_documents_carry_cosine_distance_and_rrf_score(group, reports_with_embeddings, settings):
    """Verify cosine_distance is set for vector-side hits and rrf_score reflects fusion."""
    _, _, r2 = reports_with_embeddings
    dim = settings.EMBEDDINGS_DIM
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
    for prev, curr in zip(result.documents, result.documents[1:], strict=False):
        assert curr.rrf_score <= prev.rrf_score


def test_m2m_filter_does_not_duplicate_results(group, settings):
    """Reports with multiple modalities must appear exactly once when the modality
    filter joins the M2M table. Without `.distinct()` on the queryset, joining on
    report__modalities__code__in produces one row per matching modality, which
    inflates rank position and corrupts top-K slicing."""
    dim = settings.EMBEDDINGS_DIM
    r = _make_report(body="pneumothorax findings", modalities=["CT", "MR", "DX"])
    r.groups.add(group)
    ReportSearchIndex.objects.filter(report=r).update(embedding=_unit_vec(0, dim))

    node, _ = QueryParser().parse("pneumothorax")
    assert node is not None
    s = Search(
        query=node,
        filters=SearchFilters(group=group.pk, language="en", modalities=["CT", "MR", "DX"]),
        offset=0,
        limit=10,
    )
    with patch("radis.pgsearch.providers.EmbeddingClient") as MockClient:
        MockClient.return_value.__enter__.return_value = MockClient.return_value
        MockClient.return_value.__exit__.return_value = None
        MockClient.return_value.embed_query.return_value = _unit_vec(0, dim)
        result = search(s)

    matching = [d for d in result.documents if d.document_id == r.document_id]
    assert len(matching) == 1, f"Expected 1 occurrence, got {len(matching)}"


def test_search_skips_embedding_when_query_reduces_to_not(monkeypatch, group):
    """`NOT X` alone produces an empty embedding string; the provider must
    not call the embedding service and must return FTS-only results."""
    from radis.pgsearch import providers

    embed_query_calls: list[str] = []

    class FakeEC:
        def __init__(self):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def embed_query(self, text):
            embed_query_calls.append(text)
            raise AssertionError("embed_query should not be called for NOT-only query")

    monkeypatch.setattr("radis.pgsearch.providers.EmbeddingClient", FakeEC)

    node, _ = QueryParser().parse("NOT pneumothorax")
    assert node is not None
    search = Search(
        query=node, filters=SearchFilters(group=group.pk, language="en"), offset=0, limit=10
    )
    result = providers.search(search)

    assert embed_query_calls == []
    # FTS-only path still returns a SearchResult (possibly with zero hits).
    assert result is not None


def test_search_embeds_only_positive_branch_for_and_not(monkeypatch, group, settings):
    """`A AND NOT B` embeds only `A`; FTS half still enforces the exclusion."""
    embed_query_calls: list[str] = []
    dim = settings.EMBEDDINGS_DIM

    class FakeEC:
        def __init__(self):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def embed_query(self, text):
            embed_query_calls.append(text)
            # Return a valid normalized unit vector of the right dim.
            import numpy as np

            v = np.ones(dim, dtype=np.float32)
            return (v / np.linalg.norm(v)).tolist()

    monkeypatch.setattr("radis.pgsearch.providers.EmbeddingClient", FakeEC)

    from radis.pgsearch import providers

    node, _ = QueryParser().parse("pneumothorax AND NOT effusion")
    assert node is not None
    search = Search(
        query=node, filters=SearchFilters(group=group.pk, language="en"), offset=0, limit=10
    )
    providers.search(search)

    assert embed_query_calls == ["pneumothorax"]


def test_search_excludes_negated_term_from_vector_candidates(group, settings):
    """`A AND NOT B`: a doc containing B must not leak in via the vector half.

    The vector half embeds only the positive branch (`A`), so a B-containing
    doc that is semantically near `A` would otherwise enter the RRF union even
    though the FTS half excludes it via `!B`. The negation must be enforced on
    the vector candidates too."""
    dim = settings.EMBEDDINGS_DIM
    # Contains BOTH pneumothorax and effusion; its embedding sits exactly on the
    # query vector, so it is the nearest vector neighbour. `NOT effusion` must
    # keep it out of the results entirely.
    r_leak = _make_report(body="Findings: pneumothorax with a large pleural effusion.")
    r_leak.groups.add(group)
    ReportSearchIndex.objects.filter(report=r_leak).update(embedding=_unit_vec(0, dim))
    # Legitimate hit: pneumothorax, no effusion.
    r_good = _make_report(body="Findings: pneumothorax, otherwise unremarkable.")
    r_good.groups.add(group)
    ReportSearchIndex.objects.filter(report=r_good).update(embedding=_unit_vec(1, dim))

    with patch("radis.pgsearch.providers.EmbeddingClient") as MockClient:
        MockClient.return_value.__enter__.return_value = MockClient.return_value
        MockClient.return_value.__exit__.return_value = None
        MockClient.return_value.embed_query.return_value = _unit_vec(0, dim)
        result = search(_make_search("pneumothorax AND NOT effusion", group.pk))

    ids = [d.document_id for d in result.documents]
    assert r_leak.document_id not in ids, "NOT effusion must exclude the doc from the vector half"
    assert r_good.document_id in ids


def test_retrieve_excludes_negated_term_from_vector_candidates(group, settings):
    """`A AND NOT B` on the retrieve path: extraction/subscription consumers
    must not receive a B-containing doc that leaked in via the vector half."""
    dim = settings.EMBEDDINGS_DIM
    r_leak = _make_report(body="Findings: pneumothorax with a large pleural effusion.")
    r_leak.groups.add(group)
    ReportSearchIndex.objects.filter(report=r_leak).update(embedding=_unit_vec(0, dim))
    r_good = _make_report(body="Findings: pneumothorax, otherwise unremarkable.")
    r_good.groups.add(group)
    ReportSearchIndex.objects.filter(report=r_good).update(embedding=_unit_vec(1, dim))

    with patch("radis.pgsearch.providers.EmbeddingClient") as MockClient:
        MockClient.return_value.__enter__.return_value = MockClient.return_value
        MockClient.return_value.__exit__.return_value = None
        MockClient.return_value.embed_query.return_value = _unit_vec(0, dim)
        doc_ids = list(retrieve(_make_search("pneumothorax AND NOT effusion", group.pk)))

    assert r_leak.document_id not in doc_ids
    assert r_good.document_id in doc_ids


def test_or_nested_negation_is_not_excluded_globally(group, settings):
    """`(A AND NOT B) OR C`: the `NOT B` is branch-scoped. A doc matching `C`
    that also contains `B` is a legitimate hit and must NOT be dropped from the
    vector half — only top-level ANDed negations are enforced globally."""
    dim = settings.EMBEDDINGS_DIM
    # Matches the `fracture` (C) branch but also contains `effusion` (B).
    # It must survive because NOT effusion only applies to the pneumothorax branch.
    r_or = _make_report(body="Findings: acute rib fracture with small effusion.")
    r_or.groups.add(group)
    ReportSearchIndex.objects.filter(report=r_or).update(embedding=_unit_vec(0, dim))

    with patch("radis.pgsearch.providers.EmbeddingClient") as MockClient:
        MockClient.return_value.__enter__.return_value = MockClient.return_value
        MockClient.return_value.__exit__.return_value = None
        MockClient.return_value.embed_query.return_value = _unit_vec(0, dim)
        result = search(_make_search("(pneumothorax AND NOT effusion) OR fracture", group.pk))

    ids = [d.document_id for d in result.documents]
    assert r_or.document_id in ids


def test_count_matches_retrieve_union_for_semantic_only_query(group, settings):
    """count() must equal the size of retrieve()'s hybrid union. A vector-only
    hit (no FTS match) would otherwise be counted as 0, letting the extraction
    max-reports guard pass while retrieve() still yields the report."""
    dim = settings.EMBEDDINGS_DIM
    # Body does not lexically contain "pneumothorax" -> FTS misses it; the
    # embedding sits on the query vector -> it is a vector hit.
    r = _make_report(body="lungs are clear bilaterally")
    r.groups.add(group)
    ReportSearchIndex.objects.filter(report=r).update(embedding=_unit_vec(0, dim))

    with patch("radis.pgsearch.providers.EmbeddingClient") as MockClient:
        MockClient.return_value.__enter__.return_value = MockClient.return_value
        MockClient.return_value.__exit__.return_value = None
        MockClient.return_value.embed_query.return_value = _unit_vec(0, dim)
        s = _make_search("pneumothorax", group.pk)
        cnt = count(s)
        ids = list(retrieve(s))

    assert cnt == len(ids)
    assert r.document_id in ids
    assert cnt >= 1


def test_openai_rate_limit_error_falls_back_to_fts(group, reports_with_embeddings):
    """A 429 from the embedding service on the read path must trigger the FTS
    fallback, not bubble to the search view. This is the typed-openai parallel
    of test_embedding_failure_falls_back_to_fts."""
    import httpx
    import openai

    r0, _, r2 = reports_with_embeddings
    response = httpx.Response(429, request=httpx.Request("POST", "http://x"))
    rate_limit_exc = openai.RateLimitError(message="slow down", response=response, body=None)
    with patch("radis.pgsearch.providers.EmbeddingClient") as MockClient:
        MockClient.return_value.__enter__.return_value = MockClient.return_value
        MockClient.return_value.__exit__.return_value = None
        MockClient.return_value.embed_query.side_effect = rate_limit_exc
        result = search(_make_search("pneumothorax", group.pk))

    ids = [d.document_id for d in result.documents]
    # FTS-only matches come back; no exception escaped.
    assert set(ids) == {r0.document_id, r2.document_id}


def test_rate_limited_falls_back_to_fts(group, reports_with_embeddings):
    """RateLimited (the gate's query budget expired while a 429 backoff
    window was armed) must trigger the FTS fallback like any other
    embedding failure — a rate-limited provider must not break search."""
    from radis.core.utils.rate_limit import RateLimited

    r0, _, r2 = reports_with_embeddings
    with patch("radis.pgsearch.providers.EmbeddingClient") as MockClient:
        MockClient.return_value.__enter__.return_value = MockClient.return_value
        MockClient.return_value.__exit__.return_value = None
        MockClient.return_value.embed_query.side_effect = RateLimited()
        result = search(_make_search("pneumothorax", group.pk))

    ids = [d.document_id for d in result.documents]
    assert set(ids) == {r0.document_id, r2.document_id}


def test_openai_rate_limit_error_in_retrieve_falls_back_to_fts(group, reports_with_embeddings):
    """Same parallel for retrieve()."""
    import httpx
    import openai

    r0, _, r2 = reports_with_embeddings
    response = httpx.Response(429, request=httpx.Request("POST", "http://x"))
    rate_limit_exc = openai.RateLimitError(message="slow down", response=response, body=None)
    with patch("radis.pgsearch.providers.EmbeddingClient") as MockClient:
        MockClient.return_value.__enter__.return_value = MockClient.return_value
        MockClient.return_value.__exit__.return_value = None
        MockClient.return_value.embed_query.side_effect = rate_limit_exc
        result = retrieve(_make_search("pneumothorax", group.pk))

    # No exception escaped; FTS-only retrieve returned something.
    assert result is not None
