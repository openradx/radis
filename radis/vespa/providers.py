import logging
from typing import Any

from django.conf import settings
from vespa.io import VespaQueryResponse

from radis.rag.site import RetrievalResult
from radis.search.site import Search, SearchResult

from .utils.document_utils import document_from_vespa_response
from .utils.query_utils import build_yql_filter
from .vespa_app import (
    BM25_RANK_PROFILE,
    FUSION_RANK_PROFILE,
    RETRIEVAL_QUERY_PROFILE,
    RETRIEVAL_SUMMARY,
    SEARCH_QUERY_PROFILE,
    SEMANTIC_RANK_PROFILE,
    vespa_app,
)

logger = logging.getLogger(__name__)


def _execute_query(params: dict[str, Any]) -> VespaQueryResponse:
    if settings.VESPA_QUERY_LANGUAGE != "auto":
        params["language"] = settings.VESPA_QUERY_LANGUAGE

    logger.debug("Querying Vespa with params: %s", params)

    client = vespa_app.get_client()
    return client.query(**params)


def search_bm25(search: Search) -> SearchResult:
    yql = "select * from sources * where userQuery()"
    filters = build_yql_filter(search.filters)
    if filters:
        yql += f" {filters}"

    response = _execute_query(
        {
            "yql": yql,
            "query": search.query,
            "type": "web",
            "hits": search.size,
            "offset": search.offset,
            "queryProfile": SEARCH_QUERY_PROFILE,
            "ranking": BM25_RANK_PROFILE,
        }
    )

    return SearchResult(
        total_count=response.json["root"]["fields"]["totalCount"],
        coverage=response.json["root"]["coverage"]["coverage"],
        documents=[document_from_vespa_response(hit) for hit in response.hits],
    )


def search_semantic(search: Search) -> SearchResult:
    yql = "select * from sources * where userQuery()"
    filters = build_yql_filter(search.filters)
    if filters:
        yql += f" {filters}"

    response = _execute_query(
        {
            "yql": yql,
            "query": search.query,
            "type": "web",
            "hits": search.size,
            "offset": search.offset,
            "queryProfile": SEARCH_QUERY_PROFILE,
            "ranking": SEMANTIC_RANK_PROFILE,
            "body": {"input.query(q)": f"embed({search.query})"},
        }
    )

    return SearchResult(
        total_count=response.json["root"]["fields"]["totalCount"],
        coverage=response.json["root"]["coverage"]["coverage"],
        documents=[document_from_vespa_response(hit) for hit in response.hits],
    )


# https://pyvespa.readthedocs.io/en/latest/getting-started-pyvespa.html#Hybrid-search-with-the-OR-query-operator
def search_hybrid(search: Search) -> SearchResult:
    yql = "select * from sources * where userQuery()"
    filters = build_yql_filter(search.filters)
    if filters:
        yql += f" {filters}"

    response = _execute_query(
        {
            "yql": yql,
            "query": search.query,
            "type": "web",
            "hits": search.size,
            "offset": search.offset,
            "queryProfile": SEARCH_QUERY_PROFILE,
            "ranking": FUSION_RANK_PROFILE,
            "body": {"input.query(q)": f"embed({search.query})"},
        }
    )

    return SearchResult(
        total_count=response.json["root"]["fields"]["totalCount"],
        coverage=response.json["root"]["coverage"]["coverage"],
        documents=[document_from_vespa_response(hit) for hit in response.hits],
    )


def retrieve_bm25(search: Search) -> RetrievalResult:
    yql = "select * from sources * where userQuery()"
    filters = build_yql_filter(search.filters)
    if filters:
        yql += f" {filters}"

    response = _execute_query(
        {
            "yql": yql,
            "query": search.query,
            "type": "web",
            "hits": search.size,
            "offset": search.offset,
            "queryProfile": RETRIEVAL_QUERY_PROFILE,
            "ranking": "unranked",
            "sorting": "-study_datetime",
            "summary": RETRIEVAL_SUMMARY,
        },
    )

    return RetrievalResult(
        total_count=response.json["root"]["fields"]["totalCount"],
        coverage=response.json["root"]["coverage"]["coverage"],
        document_ids=[hit["fields"]["document_id"] for hit in response.hits],
    )
