import logging
from typing import Iterator, cast

from django.conf import settings
from django.contrib.postgres.search import SearchHeadline, SearchQuery, SearchRank
from django.db.models import F, Q
from pgvector.django import CosineDistance

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
from .utils.embedding_client import EmbeddingClient
from .utils.language_utils import code_to_language

logger = logging.getLogger(__name__)


def sanitize_term(term: str) -> str:
    return "".join(char for char in term if is_search_token_char(char))


def _resolve_language(filters: SearchFilters) -> str:
    return code_to_language(filters.language)


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


def _fts_search(search: Search) -> SearchResult:
    """Full-text search only (original behavior)."""
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


def _hybrid_search(search: Search) -> SearchResult:
    """Hybrid search combining full-text search with semantic vector similarity via RRF."""
    query_str = _build_query_string(search.query)
    language = _resolve_language(search.filters)
    fts_query = SearchQuery(query_str, search_type="raw", config=language)
    filter_query = _build_filter_query(search.filters)

    # Generate query embedding
    client = EmbeddingClient()
    query_embedding = client.embed_single(search.query_text)

    # Get FTS results with ranks
    fts_results = list(
        ReportSearchVector.objects.filter(filter_query)
        .filter(search_vector=fts_query)
        .annotate(rank=SearchRank(F("search_vector"), fts_query))
        .select_related("report")
        .order_by("-rank")
        .values_list("id", flat=True)
    )

    # Get vector similarity results (only reports that have embeddings)
    vector_results = list(
        ReportSearchVector.objects.filter(filter_query)
        .filter(embedding__isnull=False)
        .annotate(distance=CosineDistance("embedding", query_embedding))
        .order_by("distance")
        .values_list("id", flat=True)
    )

    # Build rank maps (1-indexed positions)
    fts_rank_map: dict[int, int] = {}
    for position, pk in enumerate(fts_results, start=1):
        fts_rank_map[pk] = position

    vector_rank_map: dict[int, int] = {}
    for position, pk in enumerate(vector_results, start=1):
        vector_rank_map[pk] = position

    # Compute RRF scores for the union of all results
    rrf_k = settings.HYBRID_SEARCH_RRF_K
    all_pks = set(fts_rank_map.keys()) | set(vector_rank_map.keys())
    rrf_scores: dict[int, float] = {}
    for pk in all_pks:
        score = 0.0
        if pk in fts_rank_map:
            score += 1.0 / (rrf_k + fts_rank_map[pk])
        if pk in vector_rank_map:
            score += 1.0 / (rrf_k + vector_rank_map[pk])
        rrf_scores[pk] = score

    # Sort by RRF score descending
    sorted_pks = sorted(rrf_scores.keys(), key=lambda pk: rrf_scores[pk], reverse=True)
    total_count = len(sorted_pks)

    # Apply offset/limit
    if search.limit is None:
        page_pks = sorted_pks[search.offset :]
    else:
        page_pks = sorted_pks[search.offset : search.offset + search.limit]

    if not page_pks:
        return SearchResult(total_count=total_count, total_relation="exact", documents=[])

    # Fetch the actual records for the page with summaries
    results = (
        ReportSearchVector.objects.filter(id__in=page_pks)
        .annotate(
            summary=SearchHeadline(
                "report__body",
                fts_query,
                config=language,
                start_sel="<em>",
                stop_sel="</em>",
                min_words=10,
                max_words=20,
                max_fragments=10,
            )
        )
        .select_related("report")
    )

    # Build a lookup from pk to record
    record_map: dict[int, AnnotatedReportSearchVector] = {}
    for result in results:
        result.rank = rrf_scores[result.pk]  # type: ignore[attr-defined]
        record_map[result.pk] = cast(AnnotatedReportSearchVector, result)

    # Preserve RRF sort order
    documents = [
        document_from_pgsearch_response(record_map[pk]) for pk in page_pks if pk in record_map
    ]

    return SearchResult(total_count=total_count, total_relation="exact", documents=documents)


def search(search: Search) -> SearchResult:
    if search.filters.use_semantic and search.query_text:
        return _hybrid_search(search)
    return _fts_search(search)


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
