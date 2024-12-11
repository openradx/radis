import logging
from typing import Iterator, cast

import pyparsing as pp
from django.contrib.postgres.search import (
    SearchQuery,
    SearchRank,
)
from django.db.models import F, Q
from django.db.models.expressions import RawSQL

from radis.reports.models import Report
from radis.search.site import Search, SearchFilters, SearchResult
from radis.search.utils.query_parser import BinaryNode, ParensNode, QueryNode, TermNode, UnaryNode

from .models import ReportSearchVectorNew as ReportSearchVector
from .utils.document_utils import AnnotatedReportSearchVector, document_from_pgsearch_response
from .utils.language_utils import code_to_language

logger = logging.getLogger(__name__)


def sanitize_term(term: str) -> str:
    valid_chars = pp.alphanums + pp.alphas8bit + "_-'"
    return "".join(char for char in term if char in valid_chars)


def _build_query_string(node: QueryNode) -> str:
    if isinstance(node, TermNode):
        if node.term_type == "WORD":
            return node.value
        elif node.term_type == "PHRASE":
            terms = node.value.split()
            terms = [sanitize_term(term) for term in terms]
            terms = [term for term in terms if term]
            terms = [f"'{term}'" for term in terms]
            return " <-> ".join(terms)
        else:
            raise ValueError(f"Unknown term type: {node.term_type}")
    elif isinstance(node, ParensNode):
        return f"({_build_query_string(node.expression)})"
    elif isinstance(node, UnaryNode):
        assert node.operator == "NOT"
        return f"!{_build_query_string(node.operand)}"
    elif isinstance(node, BinaryNode):
        if node.operator == "AND":
            return f"{_build_query_string(node.left)} & {_build_query_string(node.right)}"
        elif node.operator == "OR":
            return f"{_build_query_string(node.left)} | {_build_query_string(node.right)}"
        else:
            raise ValueError(f"Unknown operator: {node.operator}")
    else:
        raise ValueError(f"Unknown node type: {type(node)}")


def _build_filter_query(filters: SearchFilters):
    fq = Q()

    # Apply hard filter criteria
    if filters.patient_sex:
        fq &= Q(patient_sex=filters.patient_sex)
    if filters.language:
        fq &= Q(language__code=filters.language)
    if filters.modalities:
        fq &= Q(modalities__code__in=filters.modalities)
    if filters.study_date_from:
        fq &= Q(study_datetime__date__gte=filters.study_date_from)
    if filters.study_date_till:
        fq &= Q(study_datetime__date__lte=filters.study_date_till)
    if filters.study_description:
        fq &= Q(study_description__icontains=filters.study_description)
    if filters.patient_age_from is not None:
        fq &= Q(patient_age__gte=filters.patient_age_from)
    if filters.patient_age_till is not None:
        fq &= Q(patient_age__lte=filters.patient_age_till)
    if filters.patient_id:
        fq &= Q(patient_id=filters.patient_id)
    if filters.created_after:
        fq &= Q(created_at__gte=filters.created_after)
    if filters.created_before:
        fq &= Q(created_at__lte=filters.created_before)

    return fq


def search(search: Search) -> SearchResult:
    query_str = _build_query_string(search.query)
    language = code_to_language(search.filters.language)
    query = SearchQuery(query_str, search_type="raw", config=language)
    filter_query = _build_filter_query(search.filters)
    body_field = f"body_{search.filters.language}"
    print(query_str)

    search_words = query_str.split(" & ")

    # Build the WHERE clause dynamically to include all search terms
    where_clause = " AND ".join(["reports_report.id @@@ %s" for _ in search_words])
    params = [f"{body_field}:{word}" for word in search_words]

    results = (
        Report.objects.filter(filter_query)
        .annotate(
            rank=RawSQL("paradedb.score(reports_report.id)", []),
            summary=RawSQL(
                f"paradedb.snippet(reports_report.{body_field}, start_tag => '<b><u>', end_tag => '</u></b>',  max_num_chars => 20)",
                [],
            ),
        )
        .extra(
            where=[where_clause],
            params=params,
        )
        .order_by("-rank")
    )
    # results = (
    #     Report.objects.filter(filter_query).extra(
    #         select={
    #             "rank": "paradedb.score(reports_report.id)",
    #             "summary": "paradedb.snippet(reports_report.body)",
    #         },
    #         select_params=["body:" + query_str, "body:" + query_str],
    #         where=["reports_report.id @@@ %s"],
    #         params=["body:" + query_str],
    #         order_by=["-rank"],
    #     )
    # .annotate(
    #     summary=SearchHeadline(
    #         "report__body",
    #         query_str,
    #         config=language,
    #         start_sel="<em>",
    #         stop_sel="</em>",
    #         min_words=10,
    #         max_words=20,
    #         max_fragments=10,
    #     )
    # )
    # # .select_related("report")
    # .order_by("-rank")
    # )
    # print(results[0].summary)
    total_count = results.count()
    if search.limit is None:
        results = results[search.offset :]
    else:
        results = results[search.offset : search.offset + search.limit]
    documents = [
        document_from_pgsearch_response(cast(AnnotatedReportSearchVector, result))
        for result in results
    ]

    return SearchResult(total_count=total_count, total_relation="exact", documents=documents)


def count(search: Search) -> int:
    query_str = _build_query_string(search.query)
    language = code_to_language(search.filters.language)
    query = SearchQuery(query_str, search_type="raw", config=language)
    filter_query = _build_filter_query(search.filters)
    language = code_to_language(search.filters.language)
    results = ReportSearchVector.objects.filter(filter_query).filter(search_vector=query)
    return results.count()


def retrieve(search: Search) -> Iterator[str]:
    query_str = _build_query_string(search.query)
    language = code_to_language(search.filters.language)
    query = SearchQuery(query_str, search_type="raw", config=language)
    filter_query = _build_filter_query(search.filters)
    language = code_to_language(search.filters.language)

    results = (
        ReportSearchVector.objects.filter(filter_query)
        .filter(search_vector=query)
        .annotate(
            rank=SearchRank(
                F("search_vector_new"),
                query,
            )
        )
        .select_related("report")
        .order_by("-rank")
        .values_list("report__document_id", flat=True)
    )

    return results.iterator()
