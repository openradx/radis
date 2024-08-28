import logging
from typing import Iterator, cast

import pyparsing as pp
from django.contrib.postgres.search import (
    SearchHeadline,
    SearchQuery,
    SearchRank,
)
from django.db.models import F, Q

from radis.search.site import Search, SearchFilters, SearchResult
from radis.search.utils.query_parser import BinaryNode, ParensNode, QueryNode, TermNode, UnaryNode

from .models import ReportSearchVector
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
        fq &= Q(report__patient_sex=filters.patient_sex)
    if filters.language:
        fq &= Q(report__language__code=filters.language)
    if filters.modalities:
        fq &= Q(report__modalities__code__in=filters.modalities)
    if filters.study_date_from:
        fq &= Q(report__study_datetime__gte=filters.study_date_from)
    if filters.study_date_till:
        fq &= Q(report__study_datetime__lte=filters.study_date_till)
    if filters.study_description:
        fq &= Q(report__study_description__icontains=filters.study_description)
    if filters.patient_age_from is not None:
        fq &= Q(report__patient_age__gte=filters.patient_age_from)
    if filters.patient_age_till is not None:
        fq &= Q(report__patient_age__lte=filters.patient_age_till)
    if filters.patient_id:
        fq &= Q(report__patient_id=filters.patient_id)
    if filters.created_after:
        fq &= Q(report__created_at__gte=filters.created_after)
    if filters.created_before:
        fq &= Q(report__created_at__lte=filters.created_before)

    return fq


def search(search: Search) -> SearchResult:
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
                F("search_vector"),
                query,
            )
        )
        .annotate(
            summary=SearchHeadline(
                "report__body",
                query,
                config=language,
                start_sel="<em>",
                stop_sel="</em>",
                min_words=10,
                max_words=20,
                max_fragments=10,
            )
        )
        .select_related("report")
        .order_by("-rank")
    )

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
                F("search_vector"),
                query,
            )
        )
        .select_related("report")
        .order_by("-rank")
        .values_list("report__document_id", flat=True)
    )

    return results.iterator()
