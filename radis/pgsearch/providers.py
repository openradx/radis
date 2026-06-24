import logging
import unicodedata
from collections.abc import Iterator
from typing import cast

from django.contrib.postgres.search import SearchHeadline, SearchQuery, SearchRank
from django.db.models import F, Q

from radis.search.site import Search, SearchFilters, SearchResult
from radis.search.utils.query_parser import (
    BinaryNode,
    ParensNode,
    QueryNode,
    TermNode,
    UnaryNode,
    is_search_token_char,
)

from .models import ReportSearchVector
from .utils.document_utils import AnnotatedReportSearchVector, document_from_pgsearch_response
from .utils.language_utils import code_to_language

logger = logging.getLogger(__name__)


def sanitize_term(term: str) -> str:
    return "".join(char for char in term if is_search_token_char(char))


def _has_lexeme_char(term: str) -> bool:
    """Whether ``term`` contains at least one letter, digit or mark.

    Tokens made up solely of "safe" punctuation (e.g. a lone apostrophe ``'``)
    carry no lexeme and must not be emitted into a ``to_tsquery(..., 'raw')``
    string, where they trigger a Postgres ``ProgrammingError`` (syntax error in
    tsquery). Real words with embedded apostrophes (``don't``, ``it's``) still
    contain letters and are kept.
    """
    return any(unicodedata.category(char)[0] in ("L", "N", "M") for char in term)


def _quote_term(term: str) -> str:
    """Render ``term`` as a single-quoted raw-tsquery lexeme.

    Embedded apostrophes are doubled so that a term like ``don't`` becomes the
    valid lexeme ``'don''t'`` instead of prematurely closing the quote and
    producing a tsquery syntax error.
    """
    return "'" + term.replace("'", "''") + "'"


def _resolve_language(filters: SearchFilters) -> str:
    return code_to_language(filters.language)


def _build_query_string(node: QueryNode) -> str:
    if isinstance(node, TermNode):
        if node.term_type == "WORD":
            term = sanitize_term(node.value)
            # A token carrying no lexeme (e.g. a lone apostrophe) must not reach
            # the raw tsquery, where it is a syntax error; drop it to an empty
            # query fragment that matches nothing instead of crashing.
            if not _has_lexeme_char(term):
                return ""
            return _quote_term(term)
        elif node.term_type == "PHRASE":
            terms = node.value.split()
            terms = [sanitize_term(term) for term in terms]
            terms = [term for term in terms if _has_lexeme_char(term)]
            terms = [_quote_term(term) for term in terms]
            return " <-> ".join(terms)
        else:
            raise ValueError(f"Unknown term type: {node.term_type}")
    elif isinstance(node, ParensNode):
        inner = _build_query_string(node.expression)
        # Drop empty groups so we never emit a bare "()" into the raw tsquery.
        return f"({inner})" if inner else ""
    elif isinstance(node, UnaryNode):
        assert node.operator == "NOT"
        operand = _build_query_string(node.operand)
        # A negation of nothing is nothing; emitting "!" alone is a syntax error.
        return f"!{operand}" if operand else ""
    elif isinstance(node, BinaryNode):
        if node.operator not in ("AND", "OR"):
            raise ValueError(f"Unknown operator: {node.operator}")
        left = _build_query_string(node.left)
        right = _build_query_string(node.right)
        # If a side collapsed to nothing (e.g. a lone-apostrophe token was
        # dropped), don't leave a dangling "&"/"|" operator behind -- fall back
        # to whichever side survived (or nothing if neither did).
        if not left:
            return right
        if not right:
            return left
        operator = "&" if node.operator == "AND" else "|"
        return f"{left} {operator} {right}"
    else:
        raise ValueError(f"Unknown node type: {type(node)}")


def _build_filter_query(filters: SearchFilters) -> Q:
    fq = Q()

    # Apply hard filter criteria
    if filters.patient_sex:
        fq &= Q(report__patient_sex=filters.patient_sex)
    if filters.language:
        fq &= Q(report__language__code=filters.language)
    if filters.modalities:
        fq &= Q(report__modalities__code__in=filters.modalities)
    if filters.study_date_from:
        fq &= Q(report__study_datetime__date__gte=filters.study_date_from)
    if filters.study_date_till:
        fq &= Q(report__study_datetime__date__lte=filters.study_date_till)
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
    language = _resolve_language(search.filters)
    query = SearchQuery(query_str, search_type="raw", config=language)
    filter_query = _build_filter_query(search.filters)
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
    language = _resolve_language(search.filters)
    query = SearchQuery(query_str, search_type="raw", config=language)
    filter_query = _build_filter_query(search.filters)
    results = ReportSearchVector.objects.filter(filter_query).filter(search_vector=query)
    return results.count()


def retrieve(search: Search) -> Iterator[str]:
    query_str = _build_query_string(search.query)
    language = _resolve_language(search.filters)
    query = SearchQuery(query_str, search_type="raw", config=language)
    filter_query = _build_filter_query(search.filters)
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


def filter(filter: SearchFilters) -> Iterator[str]:
    filter_query = _build_filter_query(filter)
    results = ReportSearchVector.objects.filter(filter_query).values_list(
        "report__document_id", flat=True
    )
    return results.iterator()
