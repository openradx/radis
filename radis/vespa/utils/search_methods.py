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
