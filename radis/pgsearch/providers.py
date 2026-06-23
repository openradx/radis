import logging
from typing import Iterator, Literal, cast

from django.conf import settings
from django.contrib.postgres.search import SearchHeadline, SearchQuery, SearchRank
from django.db.models import F, Q
from pgvector.django import CosineDistance

from radis.reports.models import Report
from radis.search.site import ReportDocument, Search, SearchFilters, SearchResult
from radis.search.utils.query_parser import (
    BinaryNode,
    ParensNode,
    QueryNode,
    QueryParser,
    TermNode,
    UnaryNode,
    is_search_token_char,
)

from .models import ReportSearchIndex
from .utils.document_utils import AnnotatedReportSearchIndex, document_from_pgsearch_response
from .utils.embedding_client import EmbeddingClient, EmbeddingClientError
from .utils.fusion import rrf_fuse, summary_with_fallback
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


def search(search: Search) -> SearchResult:
    query_str = _build_query_string(search.query)
    language = _resolve_language(search.filters)
    filter_query = _build_filter_query(search.filters)
    tsquery = SearchQuery(query_str, search_type="raw", config=language)

    # Vector side: strip NOT branches (see spec §7.8). If nothing is left,
    # skip the embedding call entirely and fall through to FTS-only.
    query_text = QueryParser.unparse_for_embedding(search.query)
    query_vec: list[float] | None = None
    if query_text.strip():
        try:
            with EmbeddingClient() as ec:
                query_vec = ec.embed_query(query_text)
        except EmbeddingClientError as e:
            logger.warning("Hybrid search falling back to FTS-only: %s", e)
            query_vec = None

    vec_rank: dict[int, int] = {}
    vec_distance: dict[int, float] = {}
    if query_vec is not None:
        vec_rows = list(
            ReportSearchIndex.objects.filter(filter_query)
            .distinct()
            .exclude(embedding__isnull=True)
            .annotate(distance=CosineDistance("embedding", query_vec))
            .order_by("distance", "report_id")
            .values_list("report_id", "distance")[: settings.HYBRID_VECTOR_TOP_K]
        )
        for i, (rid, dist) in enumerate(vec_rows):
            vec_rank[rid] = i + 1
            vec_distance[rid] = float(dist)

    # FTS side: bounded set, ts_rank only (no headline at this stage).
    fts_rows = list(
        ReportSearchIndex.objects.filter(filter_query)
        .distinct()
        .filter(search_vector=tsquery)
        .annotate(rank=SearchRank(F("search_vector"), tsquery))
        .order_by("-rank", "report_id")
        .values("report_id", "rank")[: settings.HYBRID_FTS_MAX_RESULTS]
    )
    fts_rank = {row["report_id"]: i + 1 for i, row in enumerate(fts_rows)}

    # Fusion.
    ordered_pairs = rrf_fuse(vec_rank, fts_rank, k=settings.HYBRID_RRF_K)
    rrf_score_by_id = dict(ordered_pairs)
    ordered_ids = list(rrf_score_by_id)
    total_count = len(ordered_ids)
    total_relation: Literal["exact", "at_least", "approximately"] = (
        "at_least"
        if (
            len(fts_rows) >= settings.HYBRID_FTS_MAX_RESULTS
            or len(vec_rank) >= settings.HYBRID_VECTOR_TOP_K
        )
        else "exact"
    )

    if search.limit is None:
        page_ids = ordered_ids[search.offset :]
    else:
        page_ids = ordered_ids[search.offset : search.offset + search.limit]

    # Headline + hydration for the page slice only.
    page_rows = (
        ReportSearchIndex.objects.filter(report_id__in=page_ids)
        .annotate(
            summary=SearchHeadline(
                "report__body",
                tsquery,
                config=language,
                start_sel="<em>",
                stop_sel="</em>",
                min_words=10,
                max_words=20,
                max_fragments=10,
            ),
            rank=SearchRank(F("search_vector"), tsquery),
        )
        .select_related("report")
    )
    by_id = {r.report.pk: r for r in page_rows}

    documents: list[ReportDocument] = []
    for rid in page_ids:
        rsv = by_id.get(rid)
        if rsv is None:
            continue
        rsv.summary = summary_with_fallback(  # type: ignore[attr-defined]
            rsv.report.body, rsv.summary or "", max_words=30  # type: ignore[attr-defined]
        )
        documents.append(
            document_from_pgsearch_response(
                cast(AnnotatedReportSearchIndex, rsv),
                cosine_distance=vec_distance.get(rid),
                rrf_score=rrf_score_by_id.get(rid, 0.0),
            )
        )

    return SearchResult(
        total_count=total_count, total_relation=total_relation, documents=documents
    )


def count(search: Search) -> int:
    query_str = _build_query_string(search.query)
    language = _resolve_language(search.filters)
    query = SearchQuery(query_str, search_type="raw", config=language)
    filter_query = _build_filter_query(search.filters)
    results = ReportSearchIndex.objects.filter(filter_query).filter(search_vector=query)
    return results.count()


def retrieve(search: Search) -> Iterator[str]:
    query_str = _build_query_string(search.query)
    language = _resolve_language(search.filters)
    filter_query = _build_filter_query(search.filters)
    tsquery = SearchQuery(query_str, search_type="raw", config=language)

    # Vector side: strip NOT branches (see spec §7.8). If nothing is left,
    # skip the embedding call entirely and fall through to FTS-only.
    query_text = QueryParser.unparse_for_embedding(search.query)
    query_vec: list[float] | None = None
    if query_text.strip():
        try:
            with EmbeddingClient() as ec:
                query_vec = ec.embed_query(query_text)
        except EmbeddingClientError as e:
            logger.warning("Hybrid retrieve falling back to FTS-only: %s", e)
            query_vec = None

    vec_rank: dict[int, int] = {}
    if query_vec is not None:
        vec_ids = list(
            ReportSearchIndex.objects.filter(filter_query)
            .distinct()
            .exclude(embedding__isnull=True)
            .annotate(distance=CosineDistance("embedding", query_vec))
            .order_by("distance", "report_id")
            .values_list("report_id", flat=True)[: settings.HYBRID_VECTOR_TOP_K]
        )
        vec_rank = {rid: i + 1 for i, rid in enumerate(vec_ids)}

    fts_rows = list(
        ReportSearchIndex.objects.filter(filter_query)
        .distinct()
        .filter(search_vector=tsquery)
        .annotate(rank=SearchRank(F("search_vector"), tsquery))
        .order_by("-rank", "report_id")
        .values("report_id", "rank")[: settings.HYBRID_FTS_MAX_RESULTS]
    )
    fts_rank = {row["report_id"]: i + 1 for i, row in enumerate(fts_rows)}

    ordered_ids = [rid for rid, _ in rrf_fuse(vec_rank, fts_rank, k=settings.HYBRID_RRF_K)]
    if not ordered_ids:
        return iter([])

    id_to_doc = dict(
        Report.objects.filter(pk__in=ordered_ids).values_list("pk", "document_id")
    )
    return (id_to_doc[rid] for rid in ordered_ids if rid in id_to_doc)


def filter(filter: SearchFilters) -> Iterator[str]:
    filter_query = _build_filter_query(filter)
    results = ReportSearchIndex.objects.filter(filter_query).values_list(
        "report__document_id", flat=True
    )
    return results.iterator()
