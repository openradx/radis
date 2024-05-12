import logging
from datetime import datetime, time
from typing import Any, Iterator

from vespa.io import VespaQueryResponse

from radis.search.site import Search, SearchFilters, SearchResult

from .utils.document_utils import document_from_vespa_response
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


def _build_yql_filter(filters: SearchFilters) -> str:
    print(filters)
    q = f" and groups = {filters.group}"
    q += f" and language contains '{filters.language}'"
    if filters.study_date_from:
        df = datetime.combine(filters.study_date_from, time()).timestamp()
        q += f" and study_datetime > {df}"
    if filters.study_date_till:
        dt = datetime.combine(filters.study_date_till, time()).timestamp()
        q += f" and study_datetime < {dt}"
    if filters.study_description:
        q += f" and study_description contains '{filters.study_description}'"
    if filters.modalities:
        modalities = [f"'{m}'" for m in filters.modalities]
        q += f" and modalities in ({','.join(modalities)})"
    if filters.patient_sex:
        q += f" and patient_sex contains '{filters.patient_sex}'"
    if filters.patient_age_from:
        q += f" and patient_age > {filters.patient_age_from}"
    if filters.patient_age_till and filters.patient_age_till < 120:
        q += f" and patient_age < {filters.patient_age_till}"
    return q


def _execute_query(params: dict[str, Any]) -> VespaQueryResponse:
    logger.debug("Querying Vespa with params:\n%s", params)

    client = vespa_app.get_client()
    response = client.query(**params)

    coverage = response.json["root"]["coverage"]["coverage"]
    if coverage < 100:
        logger.warning(f"Coverage of query is only {coverage}%")

    logger.debug("Received Vespa response:\n%s", response.get_json())

    return response


def search_bm25(search: Search) -> SearchResult:
    yql = "select * from sources * where userQuery()"
    yql += _build_yql_filter(search.filters)

    response = _execute_query(
        {
            "yql": yql,
            "query": search.query,
            "type": "web",
            "hits": search.limit,
            "offset": search.offset,
            "queryProfile": SEARCH_QUERY_PROFILE,
            "language": search.filters.language,
            "ranking": BM25_RANK_PROFILE,
        }
    )

    return SearchResult(
        total_count=response.json["root"]["fields"]["totalCount"],
        total_relation="exact",
        documents=[document_from_vespa_response(hit) for hit in response.hits],
    )


def search_semantic(search: Search) -> SearchResult:
    yql = "select * from sources * where userQuery()"
    yql += _build_yql_filter(search.filters)

    response = _execute_query(
        {
            "yql": yql,
            "query": search.query,
            "type": "web",
            "hits": search.limit,
            "offset": search.offset,
            "queryProfile": SEARCH_QUERY_PROFILE,
            "language": search.filters.language,
            "ranking": SEMANTIC_RANK_PROFILE,
            "body": {"input.query(q)": f"embed({search.query})"},
        }
    )

    return SearchResult(
        total_count=response.json["root"]["fields"]["totalCount"],
        total_relation="exact",
        documents=[document_from_vespa_response(hit) for hit in response.hits],
    )


# https://pyvespa.readthedocs.io/en/latest/getting-started-pyvespa.html#Hybrid-search-with-the-OR-query-operator
def search_hybrid(search: Search) -> SearchResult:
    yql = "select * from sources * where userQuery()"
    yql += _build_yql_filter(search.filters)

    response = _execute_query(
        {
            "yql": yql,
            "query": search.query,
            "type": "web",
            "hits": search.limit,
            "offset": search.offset,
            "queryProfile": SEARCH_QUERY_PROFILE,
            "language": search.filters.language,
            "ranking": FUSION_RANK_PROFILE,
            "body": {"input.query(q)": f"embed({search.query})"},
        }
    )

    return SearchResult(
        total_count=response.json["root"]["fields"]["totalCount"],
        total_relation="exact",
        documents=[document_from_vespa_response(hit) for hit in response.hits],
    )


def count_bm25(search: Search) -> int:
    yql = "select * from sources * where userQuery()"
    yql += _build_yql_filter(search.filters)

    response = _execute_query(
        {
            "yql": yql,
            "query": search.query,
            "type": "web",
            "hits": 0,
            "offset": 0,
            "queryProfile": RETRIEVAL_QUERY_PROFILE,
            "language": search.filters.language,
            "ranking": "unranked",
            "sorting": "-study_datetime",
            "summary": RETRIEVAL_SUMMARY,
        },
    )

    return response.json["root"]["fields"]["totalCount"]


def retrieve_bm25(search: Search) -> Iterator[str]:
    yql = "select * from sources * where userQuery()"
    yql += _build_yql_filter(search.filters)

    response = _execute_query(
        {
            "yql": yql,
            "query": search.query,
            "type": "web",
            "hits": search.limit,
            "offset": search.offset,
            "queryProfile": RETRIEVAL_QUERY_PROFILE,
            "language": search.filters.language,
            "ranking": "unranked",
            "sorting": "-study_datetime",
            "summary": RETRIEVAL_SUMMARY,
        },
    )

    for hit in response.hits:
        document_id = hit["fields"]["document_id"]
        yield document_id
