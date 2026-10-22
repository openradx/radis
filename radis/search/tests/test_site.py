import dataclasses

import pytest

from radis.search.site import (
    FullTextSearch,
    SearchFilters,
    SearchProvider,
    SearchResult,
    SemanticSearch,
    SimilarityRerank,
)
from radis.search.utils.query_parser import QueryParser


def _node(text: str):
    node, _fixes = QueryParser().parse(text)
    assert node is not None
    return node


def test_full_text_search_defaults():
    request = FullTextSearch(query=_node("pneumothorax"), filters=SearchFilters(group=1))
    assert request.offset == 0
    assert request.limit == 10
    assert request.rerank is None


def test_requests_are_keyword_only():
    with pytest.raises(TypeError):
        FullTextSearch(_node("pneumothorax"), SearchFilters(group=1))  # type: ignore[misc]


def test_requests_are_frozen():
    request = SemanticSearch(text="free air", filters=SearchFilters(group=1))
    with pytest.raises(dataclasses.FrozenInstanceError):
        request.text = "other"  # type: ignore[misc]


def test_blank_rerank_text_is_rejected():
    with pytest.raises(ValueError):
        SimilarityRerank("   ")


def test_blank_semantic_text_is_rejected():
    with pytest.raises(ValueError):
        SemanticSearch(text=" \n", filters=SearchFilters(group=1))


@pytest.mark.parametrize("value", [-0.1, 1.5])
def test_min_similarity_outside_the_unit_interval_is_rejected(value):
    with pytest.raises(ValueError):
        SemanticSearch(text="free air", filters=SearchFilters(group=1), min_similarity=value)


def test_min_similarity_bounds_are_accepted():
    assert (
        SemanticSearch(text="x", filters=SearchFilters(), min_similarity=0.0).min_similarity == 0.0
    )
    assert (
        SemanticSearch(text="x", filters=SearchFilters(), min_similarity=1.0).min_similarity == 1.0
    )


def test_result_and_provider_defaults_keep_stubs_constructible():
    result = SearchResult(total_count=0, total_relation="exact", documents=[])
    assert result.degraded is False
    provider = SearchProvider(name="stub", search=lambda request: result, max_results=10)
    assert provider.semantic is False
