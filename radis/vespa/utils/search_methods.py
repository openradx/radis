from radis.search.models import SearchResult

from ..vespa_app import vespa_app
from .document_utils import document_from_vespa_response


def search_bm25(query: str, offset: int, page_size: int) -> SearchResult:
    client = vespa_app.get_client()
    response = client.query(
        yql="select * from report where userQuery()",
        query=query,
        type="web",
        hits=page_size,
        offset=offset,
        ranking="bm25",
    )

    return SearchResult(
        total_count=response.json["root"]["fields"]["totalCount"],
        coverage=response.json["root"]["coverage"]["coverage"],
        documents=[document_from_vespa_response(hit) for hit in response.hits],
    )


# https://pyvespa.readthedocs.io/en/latest/getting-started-pyvespa.html#Hybrid-search-with-the-OR-query-operator
def search_hybrid(query: str, offset: int, page_size: int) -> SearchResult:
    client = vespa_app.get_client()
    response = client.query(
        yql="select * from sources * where userQuery() or \
            ({targetHits:1000}nearestNeighbor(embedding,q)) limit 5",
        query=query,
        ranking="fusion",
        body={"input.query(q)": f"embed({query})"},
    )
    return SearchResult(
        total_count=response.json["root"]["fields"]["totalCount"],
        coverage=response.json["root"]["coverage"]["coverage"],
        documents=[document_from_vespa_response(hit) for hit in response.hits],
    )
