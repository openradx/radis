import logging
from typing import Iterable

from radis.opensearch.client import get_client
from radis.opensearch.utils.document_utils import document_from_opensearch_response
from radis.search.site import Search, SearchFilters, SearchResult

logger = logging.getLogger(__name__)


def build_query_filter(filters: SearchFilters) -> list[dict]:
    qf = []

    f = {"term": {"groups": filters.group}}
    qf.append(f)

    if filters.study_date_from or filters.study_date_till:
        f = {"range": {"study_datetime": {}}}
        if filters.study_date_from:
            f["range"]["study_datetime"]["gte"] = filters.study_date_from
        if filters.study_date_till:
            f["range"]["study_datetime"]["lte"] = filters.study_date_till
        qf.append(f)

    if filters.study_description:
        f = {"wildcard": {"study_description": f"*{filters.study_description}*"}}
        qf.append(f)

    if filters.modalities:
        f = {"terms": {"modalities": filters.modalities}}
        qf.append(f)

    if filters.patient_sex:
        f = {"term": {"patient_sex": filters.patient_sex}}
        qf.append(f)

    if filters.patient_age_from or filters.patient_age_till:
        f = {"range": {"patient_age": {}}}
        if filters.patient_age_from:
            f["range"]["patient_age"]["gte"] = filters.patient_age_from
        if filters.patient_age_till:
            f["range"]["patient_age"]["lte"] = filters.patient_age_till
        qf.append(f)

    return qf


def search(search: Search) -> SearchResult:
    language = search.filters.language
    index_name = f"reports_{language}"

    body = {
        "query": {
            "bool": {
                "filter": build_query_filter(search.filters),
                "must": {
                    "simple_query_string": {
                        "query": search.query,
                    },
                },
            }
        },
        "highlight": {
            "fields": {
                "body": {},
            }
        },
        "_source": [
            "pacs_name",
            "pacs_link",
            "patient_birth_date",
            "patient_age",
            "patient_sex",
            "study_description",
            "study_datetime",
            "modalities",
        ],
        "from": search.offset,
        "size": search.limit,
    }

    logger.debug(f"Querying OpenSearch index '{index_name}' with body:\n%s", body)

    client = get_client()
    response = client.search(
        index=index_name,
        body=body,
    )

    logger.debug("Received OpenSearch response:\n%s", response)

    if response["hits"]["total"]["relation"] == "eq":
        total_relation = "exact"
    elif response["hits"]["total"]["relation"] == "gte":
        total_relation = "at_least"
    else:
        raise ValueError(f"Unsupported total relation: {response['hits']['total']['relation']}")

    return SearchResult(
        total_count=response["hits"]["total"]["value"],
        total_relation=total_relation,
        documents=[document_from_opensearch_response(hit) for hit in response["hits"]["hits"]],
    )


def _build_retrieval_query(search: Search) -> dict:
    query = {
        "query": {
            "bool": {
                "filter": build_query_filter(search.filters),
                "must": {
                    "simple_query_string": {
                        "query": search.query,
                    },
                },
            }
        },
        "_source": False,
    }

    return query


def count(search: Search) -> int:
    query = _build_retrieval_query(search)
    query["track_total_hits"] = True
    query["size"] = 0

    client = get_client()
    response = client.count(
        index=f"reports_{search.filters.language}",
        body=query,
    )

    return response["hits"]["total"]["value"]


def retrieve(search: Search) -> Iterable[str]:
    language = search.filters.language
    index_name = f"reports_{language}"

    query = _build_retrieval_query(search)
    query["size"] = 1000
    query["sort"] = [
        {"study_datetime": "asc"},
        {"_id": "asc"},
    ]

    finished = False
    counter = 0
    search_after: tuple[str, str] | None = None
    while not finished:
        if search_after:
            query["search_after"] = search_after

        client = get_client()
        response = client.search(
            index=index_name,
            body=query,
        )

        hits = response["hits"]["hits"]
        if not hits:
            finished = True
        else:
            for hit in hits:
                if search.limit is not None and counter >= search.limit:
                    finished = True
                    break
                search_after = hit["sort"]
                counter += 1
                yield hit["_id"]
