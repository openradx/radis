import logging

from django.conf import settings

from radis.search.models import SearchResult
from radis.search.site import Search
from radis.vespa.utils.query_utils import build_yql

from .utils.document_utils import document_from_vespa_response
from .vespa_app import vespa_app

logger = logging.getLogger(__name__)


def search_bm25(search: Search) -> SearchResult:
    yql = build_yql(search)
    params = {
        "yql": yql,
        "query": search.query,
        "type": "web",
        "hits": search.page_size,
        "offset": search.offset,
        "ranking": "bm25",
    }

    if settings.VESPA_QUERY_LANGUAGE != "auto":
        params["language"] = settings.VESPA_QUERY_LANGUAGE

    logger.debug("Querying Vespa with params: %s", params)

    client = vespa_app.get_client()
    response = client.query(**params)

    return SearchResult(
        total_count=response.json["root"]["fields"]["totalCount"],
        coverage=response.json["root"]["coverage"]["coverage"],
        documents=[document_from_vespa_response(hit) for hit in response.hits],
    )


# https://pyvespa.readthedocs.io/en/latest/getting-started-pyvespa.html#Hybrid-search-with-the-OR-query-operator
def search_hybrid(search: Search) -> SearchResult:
    yql = build_yql(search)
    yql += " or ({targetHits:1000}nearestNeighbor(embedding,q))"
    params = {
        "yql": yql,
        "query": search.query,
        "type": "web",
        "hits": search.page_size,
        "offset": search.offset,
        "ranking": "fusion",
        "body": {"input.query(q)": f"embed({search.query})"},
    }

    if settings.VESPA_QUERY_LANGUAGE != "auto":
        params["language"] = settings.VESPA_QUERY_LANGUAGE

    logger.debug("Querying Vespa with params: %s", params)

    client = vespa_app.get_client()
    response = client.query(**params)

    return SearchResult(
        total_count=response.json["root"]["fields"]["totalCount"],
        coverage=response.json["root"]["coverage"]["coverage"],
        documents=[document_from_vespa_response(hit) for hit in response.hits],
    )
