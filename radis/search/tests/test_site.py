import dataclasses

import pytest

from radis.search.site import (
    FullTextSearch,
    SearchFilters,
    SearchProvider,
    SearchResult,
    SemanticSearch,
    SemanticSearchUnavailable,
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


def test_requests_are_keyword_only():
    with pytest.raises(TypeError):
        FullTextSearch(_node("pneumothorax"), SearchFilters(group=1))  # type: ignore[misc]


def test_requests_are_frozen():
    request = SemanticSearch(text="free air", filters=SearchFilters(group=1))
    with pytest.raises(dataclasses.FrozenInstanceError):
        request.text = "other"  # type: ignore[misc]


def test_blank_semantic_text_is_rejected():
    with pytest.raises(ValueError):
        SemanticSearch(text=" \n", filters=SearchFilters(group=1))


def test_result_and_provider_defaults_keep_stubs_constructible():
    result = SearchResult(total_count=0, total_relation="exact", documents=[])
    assert not hasattr(result, "degraded")
    provider = SearchProvider(name="stub", search=lambda request: result, max_results=10)
    assert provider.semantic is False
    assert provider.semantic_wait() == 0.0


def test_unavailable_carries_the_wait_the_service_asked_for():
    rate_limited = SemanticSearchUnavailable("rate limited", retry_after=42.0)
    unreachable = SemanticSearchUnavailable("unreachable")
    assert rate_limited.retry_after == 42.0
    assert unreachable.retry_after is None
    assert str(unreachable) == "unreachable"
