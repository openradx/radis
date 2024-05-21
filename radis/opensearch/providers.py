import logging
from typing import Any, Iterable

from radis.opensearch.client import get_client
from radis.opensearch.utils.document_utils import document_from_opensearch_response
from radis.search.site import Search, SearchFilters, SearchResult
from radis.search.utils.query_parser import BinaryNode, ParensNode, QueryNode, TermNode, UnaryNode

logger = logging.getLogger(__name__)


def _build_filter_dict(filters: SearchFilters) -> list[dict]:
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


def _build_query_string(node: QueryNode) -> str:
    if isinstance(node, TermNode):
        if node.term_type == "WORD":
            return node.value
        elif node.term_type == "PHRASE":
            return f'"{node.value.replace('"', '\\"')}"'
        else:
            raise ValueError(f"Unknown term type: {node.term_type}")
    elif isinstance(node, ParensNode):
        return f"({_build_query_string(node.expression)})"
    elif isinstance(node, UnaryNode):
        return f"{node.operator} {_build_query_string(node.operand)}"
    elif isinstance(node, BinaryNode):
        if node.implicit:
            return f"{_build_query_string(node.left)} {_build_query_string(node.right)}"
        return (
            f"{_build_query_string(node.left)} {node.operator} "
            + f"{_build_query_string(node.right)}"
        )
    else:
        raise ValueError(f"Unknown node type: {type(node)}")


def _build_query_dict(search: Search) -> dict:
    return {
        "bool": {
            "filter": _build_filter_dict(search.filters),
            "must": {
                "simple_query_string": {
                    "query": _build_query_string(search.query),
                },
            },
        }
    }


def search(search: Search) -> SearchResult:
    language = search.filters.language
    index_name = f"reports_{language}"

    body = {
        "query": _build_query_dict(search),
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


def count(search: Search) -> int:
    client = get_client()
    response = client.count(
        index=f"reports_{search.filters.language}",
        body={
            "query": _build_query_dict(search),
        },
    )

    return response["count"]


def retrieve(search: Search) -> Iterable[str]:
    language = search.filters.language
    index_name = f"reports_{language}"

    query: dict[str, Any] = {}
    query["query"] = _build_query_dict(search)
    query["_source"] = False
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
