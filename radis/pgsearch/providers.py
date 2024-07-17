import logging
from typing import Iterable

from django.contrib.postgres.search import SearchQuery, SearchRank, SearchVector
from django.db import connection
from django.db.models import F, Q

from radis.core.constants import LANGUAGE_LABELS
from radis.search.site import Search, SearchResult
from radis.search.utils.query_parser import BinaryNode, ParensNode, QueryNode, TermNode, UnaryNode

from ..reports.models import Report
from .utils.document_utils import document_from_pgsearch_response

logger = logging.getLogger(__name__)
labels = LANGUAGE_LABELS


# def _build_filter_dict(filters: SearchFilters) -> list[dict]:
#     qf = []

#     f = {"term": {"groups": filters.group}}
#     qf.append(f)

#     if filters.study_date_from or filters.study_date_till:
#         f = {"range": {"study_datetime": {}}}
#         if filters.study_date_from:
#             f["range"]["study_datetime"]["gte"] = filters.study_date_from
#         if filters.study_date_till:
#             f["range"]["study_datetime"]["lte"] = filters.study_date_till
#         qf.append(f)

#     if filters.study_description:
#         f = {"wildcard": {"study_description": f"*{filters.study_description}*"}}
#         qf.append(f)

#     if filters.modalities:
#         f = {"terms": {"modalities": filters.modalities}}
#         qf.append(f)

#     if filters.patient_sex:
#         f = {"term": {"patient_sex": filters.patient_sex}}
#         qf.append(f)

#     if filters.patient_age_from or filters.patient_age_till:
#         f = {"range": {"patient_age": {}}}
#         if filters.patient_age_from:
#             f["range"]["patient_age"]["gte"] = filters.patient_age_from
#         if filters.patient_age_till:
#             f["range"]["patient_age"]["lte"] = filters.patient_age_till
#         qf.append(f)

#     if filters.language:
#         f = {"term": {"language": filters.language}}
#         qf.append(f)

#     return qf


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


def build_filter_query(search: Search):
    query = Q()
    query_str = _build_query_string(search.query)

    # Ensure you use a valid text search configuration
    language_config = LANGUAGE_LABELS[search.filters.language].lower()
    search_vector = SearchVector("body", config=language_config)
    search_query = None
    if query_str:
        search_query = SearchQuery(query_str, config=language_config)
        query &= Q(search_vector=search_query)

    # Apply hard filter criteria
    filters = search.filters
    if filters.patient_sex:
        query &= Q(patient_sex=filters.patient_sex)
    if filters.language:
        # query &= Q(language=LANGUAGE_LABELS[filters.language].lower())
        query &= Q(language__code=filters.language)
    if filters.modalities:
        query &= Q(modalities__code__in=filters.modalities)
    if filters.study_date_from:
        query &= Q(study_datetime__gte=filters.study_date_from)
    if filters.study_date_till:
        query &= Q(study_datetime__lte=filters.study_date_till)
    if filters.study_description:
        query &= Q(study_description__icontains=filters.study_description)
    if filters.patient_age_from is not None:
        query &= Q(patient_age__gte=filters.patient_age_from)
    if filters.patient_age_till is not None:
        query &= Q(patient_age__lte=filters.patient_age_till)

    return query, search_query, search_vector


def search(search: Search) -> SearchResult:
    with connection.cursor() as cursor:
        cursor.execute("SET max_parallel_workers_per_gather = 4;")
        cursor.execute("SET max_parallel_workers = 8;")
        cursor.execute("SET parallel_tuple_cost = 0.1;")
        cursor.execute("SET parallel_setup_cost = 1000;")
        # cursor.execute("SET work_mem = '64MB';")
        # cursor.execute("SHOW work_mem;")

    query, search_query, search_vector = build_filter_query(search)
    response = Report.objects.filter(query)
    if search_query and search_vector:
        response = response.annotate(
            search=search_vector, rank=SearchRank(F("search_vector"), search_query)
        ).order_by("-rank")
    # raise Exception(response.explain())
    return SearchResult(
        total_count=response.count(),
        total_relation="exact",
        documents=[document_from_pgsearch_response(hit) for hit in response],
    )


def count(search: Search) -> int:
    query, search_query, search_vector = build_filter_query(search)
    response = Report.objects.filter(query)
    return response.count()


def retrieve(search: Search) -> Iterable[str]:
    return []
    # language = search.filters.language
    # index_name = f"reports_{language}"

    # query: dict[str, Any] = {}
    # query["query"] = _build_query_dict(search)
    # query["_source"] = False
    # query["size"] = 1000
    # query["sort"] = [
    #     {"study_datetime": "asc"},
    #     {"_id": "asc"},
    # ]

    # finished = False
    # counter = 0
    # search_after: tuple[str, str] | None = None
    # while not finished:
    #     if search_after:
    #         query["search_after"] = search_after
    #     client = None
    #     response = client.search(
    #         index=index_name,
    #         body=query,
    #     )

    #     hits = response["hits"]["hits"]
    #     if not hits:
    #         finished = True
    #     else:
    #         for hit in hits:
    #             if search.limit is not None and counter >= search.limit:
    #                 finished = True
    #                 break
    #             search_after = hit["sort"]
    #             counter += 1
    #             yield hit["_id"]
