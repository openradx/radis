import hashlib
import json
import logging
import math
import unicodedata
from collections.abc import Iterable, Iterator
from dataclasses import asdict
from datetime import date, datetime, time, timedelta
from typing import Literal, NamedTuple, cast

import openai
from django.conf import settings
from django.contrib.postgres.search import SearchHeadline, SearchQuery, SearchRank
from django.core.cache import cache
from django.db import connection, transaction
from django.db.models import Case, F, FloatField, Q, TextField, Value, When
from django.utils import timezone
from pgvector.django import CosineDistance

from radis.core.utils.embedding_client import (
    PERMANENT_EMBEDDING_ERRORS,
    EmbeddingClient,
    EmbeddingClientError,
)
from radis.core.utils.rate_limit import RateLimited
from radis.reports.models import Language
from radis.search.site import (
    FullTextSearch,
    ReportDocument,
    Search,
    SearchFilters,
    SearchRequest,
    SearchResult,
    SemanticSearch,
)
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
from .utils.document_utils import (
    HEADLINE_START,
    HEADLINE_STOP,
    AnnotatedReportSearchIndex,
    document_from_pgsearch_response,
    summary_markup,
)
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


def _language_configs(filters: SearchFilters) -> list[tuple[str, list[str]]]:
    """The text-search configurations to match under, each with the language codes
    indexed beneath it.

    Documents are indexed with their own language's configuration
    (``ReportSearchIndex.save()``), so a query built under a different one never
    meets them: 'english' stores "effusion" as 'effus' while 'simple' looks for
    'effusion'. With a language filter the queryset is already restricted to that
    language, so one configuration covers everything. Without one, every
    configuration present in the corpus needs its own branch — anything else
    silently drops the languages it does not match.
    """
    if filters.language:
        return [(code_to_language(filters.language), [filters.language])]

    codes_by_config: dict[str, list[str]] = {}
    for code in Language.objects.values_list("code", flat=True):
        codes_by_config.setdefault(code_to_language(code), []).append(code)
    # Deterministic order keeps generated SQL stable across requests.
    return sorted(codes_by_config.items())


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


def _local_day_start(day: date) -> datetime:
    """Midnight of ``day`` in the active timezone, as an aware datetime."""
    return timezone.make_aware(datetime.combine(day, time.min))


def _group_query(group: int | None) -> Q:
    """Group-scoped access control. ``None`` is fail-closed: an empty ``group_ids``
    matches only reports assigned to no group at all."""
    if group is None:
        return Q(group_ids=[])
    return Q(group_ids__contains=[group])


def _build_filter_query(filters: SearchFilters) -> Q:
    # Every predicate here is a column on ReportSearchIndex itself -- the search
    # projection (see migration 0003) mirrors the Report fields search filters
    # on, so the candidate query stays single-table. Reintroducing a ``report__``
    # traversal restores the join and the multi-second query shape it caused;
    # test_fts_candidate_query_is_single_table guards against that.
    #
    # ``SearchView`` supplies ``group=active_group.pk``; the extraction preview may
    # pass ``group=None`` when the user has no active group.
    fq = _group_query(filters.group)

    # Apply hard filter criteria
    if filters.patient_sex:
        fq &= Q(patient_sex=filters.patient_sex)
    if filters.language:
        fq &= Q(language_code=filters.language)
    if filters.modalities:
        fq &= Q(modality_codes__overlap=filters.modalities)
    # Half-open ranges rather than ``__date``: Django compiles that lookup to
    # ``(study_datetime AT TIME ZONE ...)::date``, a function over the column
    # evaluated once per row -- millions of timezone conversions per query on
    # the sequential scan, for a predicate equal to two timestamp comparisons.
    if filters.study_date_from:
        fq &= Q(study_datetime__gte=_local_day_start(filters.study_date_from))
    if filters.study_date_till:
        fq &= Q(study_datetime__lt=_local_day_start(filters.study_date_till + timedelta(days=1)))
    if filters.study_description:
        fq &= Q(study_description__icontains=filters.study_description)
    if filters.patient_age_from is not None:
        fq &= Q(patient_age__gte=filters.patient_age_from)
    if filters.patient_age_till is not None:
        fq &= Q(patient_age__lte=filters.patient_age_till)
    if filters.patient_id:
        fq &= Q(patient_id=filters.patient_id)
    if filters.created_after:
        fq &= Q(report_created_at__gte=filters.created_after)
    if filters.created_before:
        fq &= Q(report_created_at__lte=filters.created_before)
    if filters.labels:
        from radis.labels.models import LabelResult
        from radis.reports.models import Report

        surfacing_report_ids = Report.objects.filter(
            label_results__label__name__in=filters.labels,
            label_results__value__in=LabelResult.SURFACING_VALUES,
        ).values("pk")
        fq &= Q(report_id__in=surfacing_report_ids)
    if filters.updated_after:
        fq &= Q(report_updated_at__gte=filters.updated_after)

    return fq


# (EMBEDDINGS_BASE_URL, model) pairs that have already logged a full permanent-failure
# traceback in this process. Failures are deliberately not cached (see
# _embed_query_cached), so without this a persistent misconfiguration would log a full
# traceback on every single search request, forever; membership here downgrades the
# repeat occurrences to a one-line WARNING. A config change is a new key, so it still
# gets its own fresh traceback. Bounded by the number of distinct embedding
# configurations this process observes -- realistically one, since it only changes on
# a redeploy -- so this cannot grow without bound.
_LOGGED_PERMANENT_FAILURE_CONFIGS: set[tuple[str, str | None]] = set()


def _embedding_config_key() -> tuple[str, str | None]:
    spec = settings.EMBEDDINGS_MODEL
    return (settings.EMBEDDINGS_BASE_URL, spec.model if spec is not None else None)


def _embed_query_or_none(query_text: str, caller: str) -> list[float] | None:
    """Embed the text, or return None to signal a degraded search.

    The caller flags a missing vector as degraded, so every failure returns None
    instead of raising — but at different log levels: transient conditions
    (rate limiting, connection blips, 5xx) are expected under load and log a
    WARNING, while permanent misconfiguration logs a full exception (once per
    configuration, see _LOGGED_PERMANENT_FAILURE_CONFIGS) so it reaches operators;
    the notice on the page does not name the cause."""
    try:
        with EmbeddingClient(timeout=settings.EMBEDDINGS_QUERY_REQUEST_TIMEOUT_SECONDS) as ec:
            return ec.embed_query(query_text)
    except (EmbeddingClientError, *PERMANENT_EMBEDDING_ERRORS) as exc:
        config_key = _embedding_config_key()
        if config_key not in _LOGGED_PERMANENT_FAILURE_CONFIGS:
            _LOGGED_PERMANENT_FAILURE_CONFIGS.add(config_key)
            logger.exception("%s unavailable; embedding service looks misconfigured", caller)
        else:
            base_url, model = config_key
            # Include the concrete exception type/message: the suppressed traceback that
            # would otherwise explain *what* changed (wrong path vs. expired key vs. bad
            # request) is long gone from recent logs by the time this branch fires again.
            logger.warning(
                "%s unavailable; embedding service still misconfigured for this "
                "configuration (check EMBEDDINGS_BASE_URL=%r and EMBEDDINGS_MODEL=%r): "
                "%s: %s -- the search runs degraded until this is fixed; the full "
                "traceback was already logged once for this configuration",
                caller,
                base_url,
                model,
                type(exc).__name__,
                exc,
            )
        return None
    except (RateLimited, openai.OpenAIError) as e:
        logger.warning("%s unavailable: %s", caller, e)
        return None


def _embed_query_cached(query_text: str, caller: str) -> list[float] | None:
    """Cache wrapper around `_embed_query_or_none`.

    A semantic search whose filters change misses the ranking cache but still
    embeds the same text, and that vector must not be fetched again within the
    TTL. The key covers everything that determines the vector — endpoint,
    model, spec parameters, instruction, dim, query text — because a shared cache
    backend (production uses the database) outlives process restarts and thus
    config changes: "qwen3" on one deployment and "qwen3" on another are different
    weights, so the endpoint has to be part of the fingerprint too (the API key
    is not identity and stays out of the cache key). Failures are not cached: a
    transient outage must not pin searches to a degraded result for the TTL.
    """
    spec = settings.EMBEDDINGS_MODEL
    if spec is None:
        return None
    fingerprint = "\x00".join(
        [
            settings.EMBEDDINGS_BASE_URL,
            spec.model,
            json.dumps(spec.params, sort_keys=True),
            settings.EMBEDDINGS_QUERY_INSTRUCTION,
            str(settings.EMBEDDINGS_DIM),
            query_text,
        ]
    )
    key = "pgsearch-query-embedding-" + hashlib.sha256(fingerprint.encode()).hexdigest()
    vec = cache.get(key)
    if vec is None:
        vec = _embed_query_or_none(query_text, caller)
        if vec is not None:
            cache.set(key, vec, timeout=settings.EMBEDDINGS_QUERY_CACHE_TIMEOUT_SECONDS)
    return vec


def _fts_candidate_queryset(filter_query: Q, configs: list[tuple[str, list[str]]], query_str: str):
    """The FTS candidate query: the single-table scan this whole design exists for.

    Kept as its own function rather than inlined in ``_keyword_ranking_uncached``
    so that ``test_fts_candidate_query_is_single_table`` can assert on the SQL and
    the ``EXPLAIN`` plan of exactly the queryset production runs -- filters, tsquery
    match, rank annotation, ordering and bound included, not a re-creation of
    it. Everything referenced here must be a column on ``ReportSearchIndex``
    itself: one lookup or annotation traversing the ``report`` relation restores
    the joins and the multi-second query shape (see migration 0003).
    """
    tsqueries = {
        config: SearchQuery(query_str, search_type="raw", config=config) for config, _ in configs
    }

    # A document matches only under the configuration it was indexed with.
    match_q = Q()
    for config, codes in configs:
        match_q |= Q(language_code__in=codes, search_vector=tsqueries[config])

    # Rank strictly under the document's own configuration. Do not swap this for
    # Greatest over all configurations: a foreign config's ts_rank is not
    # reliably ~0 (a term whose stem equals itself scores the same under every
    # config), and Greatest would also need a special case for one branch.
    rank_expr = Case(
        *[
            When(
                language_code__in=codes,
                then=SearchRank(F("search_vector"), tsqueries[config]),
            )
            for config, codes in configs
        ],
        default=Value(0.0),
        output_field=FloatField(),
    )

    # No .distinct(): with no joins there is nothing to deduplicate, and it
    # would block the top-N heapsort that keeps this query sub-second.
    return (
        ReportSearchIndex.objects.filter(filter_query)
        .filter(match_q)
        .annotate(rank=rank_expr)
        .order_by("-rank", "report_id")
        .values("report_id", "rank")[: settings.SEARCH_FTS_MAX_RESULTS]
    )


class _KeywordRanking(NamedTuple):
    """The keyword layer: every hit of the tsquery in ts_rank order."""

    ids: list[int]
    ts_rank_by_id: dict[int, float]
    relation: Literal["exact", "at_least", "approximately"]
    configs: list[tuple[str, list[str]]]
    query_str: str


class Ranking(NamedTuple):
    """One mode's ordered result list, with the per-report values and the text-search
    configurations its documents are built from."""

    ids: list[int]
    score_by_id: dict[int, float]
    similarity_by_id: dict[int, float]
    relation: Literal["exact", "at_least", "approximately"]
    configs: list[tuple[str, list[str]]]
    query_str: str
    degraded: bool


# Bumped whenever a cached payload's shape changes, so entries written by an older
# code version are missed rather than misread.
_RANKING_CACHE_SCHEMA = 2


def _embedding_fingerprint() -> list[object] | None:
    spec = settings.EMBEDDINGS_MODEL
    if spec is None:
        return None
    return [
        settings.EMBEDDINGS_BASE_URL,
        spec.model,
        spec.params,
        settings.EMBEDDINGS_QUERY_INSTRUCTION,
        settings.EMBEDDINGS_DIM,
    ]


def _cache_key(**parts: object) -> str:
    """A shared cache backend (production uses the database) outlives process restarts
    and configuration changes, so everything that determines a ranking is part of the
    key, hashed so a 1,000-character description cannot overflow it."""
    fingerprint = json.dumps(
        {"schema": _RANKING_CACHE_SCHEMA, **parts}, sort_keys=True, default=str
    )
    return "pgsearch-ranking-" + hashlib.sha256(fingerprint.encode()).hexdigest()


def _keyword_ranking(request: FullTextSearch) -> _KeywordRanking:
    """The keyword layer, cached whenever the cache is on. It never depends on the
    embedding service, so a search and an extraction over the same query and filters
    share one entry."""
    timeout = settings.SEARCH_RANKING_CACHE_TIMEOUT_SECONDS
    key = _cache_key(
        layer="keyword",
        query=QueryParser.unparse(request.query),
        filters=asdict(request.filters),
        fts_max=settings.SEARCH_FTS_MAX_RESULTS,
    )
    if timeout > 0:
        cached = cache.get(key)
        if cached is not None:
            return _KeywordRanking(
                ids=list(cached["ids"]),
                ts_rank_by_id=dict(zip(cached["ids"], cached["ranks"], strict=True)),
                relation=cached["relation"],
                configs=[(config, list(codes)) for config, codes in cached["configs"]],
                query_str=cached["query_str"],
            )
    ranking = _keyword_ranking_uncached(request)
    if timeout > 0:
        cache.set(
            key,
            {
                "ids": ranking.ids,
                "ranks": [ranking.ts_rank_by_id[rid] for rid in ranking.ids],
                "relation": ranking.relation,
                "configs": ranking.configs,
                "query_str": ranking.query_str,
            },
            timeout=timeout,
        )
    return ranking


def _keyword_ranking_uncached(request: FullTextSearch) -> _KeywordRanking:
    query_str = _build_query_string(request.query)
    configs = _language_configs(request.filters)
    filter_query = _build_filter_query(request.filters)
    # An empty ``configs`` means an empty corpus (Report.language is a required FK),
    # and an empty match would match every row rather than none, so skip it.
    rows = [] if not configs else list(_fts_candidate_queryset(filter_query, configs, query_str))
    ids = [row["report_id"] for row in rows]
    relation: Literal["exact", "at_least", "approximately"] = (
        "at_least" if len(rows) >= settings.SEARCH_FTS_MAX_RESULTS else "exact"
    )
    return _KeywordRanking(
        ids=ids,
        ts_rank_by_id={row["report_id"]: float(row["rank"]) for row in rows},
        relation=relation,
        configs=configs,
        query_str=query_str,
    )


def _finite_similarities(rows: Iterable[tuple[int, float]]) -> dict[int, float]:
    """Similarity, 1 - cosine distance, per report id in row order."""
    measured = ((rid, 1.0 - float(distance)) for rid, distance in rows)
    # pgvector's cosine distance to a zero vector is NaN, and such a report counts as
    # not embedded: it keeps no similarity on the page or in a cached payload. Only an
    # HNSW scan leaves it out by itself; every other plan returns it.
    return {rid: value for rid, value in measured if math.isfinite(value)}


def _rank_fulltext(request: FullTextSearch) -> Ranking:
    keyword = _keyword_ranking(request)
    return Ranking(
        ids=list(keyword.ids),
        score_by_id=dict(keyword.ts_rank_by_id),
        similarity_by_id={},
        relation=keyword.relation,
        configs=keyword.configs,
        query_str=keyword.query_str,
        degraded=False,
    )


ITERATIVE_SCAN_SQL = "SET LOCAL hnsw.iterative_scan = strict_order"


def _semantic_scan_queryset(filter_query: Q, vec: list[float]):
    """The nearest reports under the filters: one HNSW index scan, capped at the beam
    width, which is the semantic depth. The LIMIT must stay in the query: without one
    the planner abandons the index for a sequential sort."""
    return (
        ReportSearchIndex.objects.filter(filter_query)
        .exclude(embedding__isnull=True)
        .annotate(distance=CosineDistance("embedding", vec))
        .order_by("distance", "report_id")
        .values_list("report_id", "distance")[: settings.SEARCH_HNSW_EF_SEARCH]
    )


def _semantic_ranking(ids: list[int], similarity: dict[int, float], *, degraded: bool) -> Ranking:
    return Ranking(
        ids=ids,
        score_by_id=dict(similarity),
        similarity_by_id=similarity,
        relation="exact",
        configs=[],
        query_str="",
        degraded=degraded,
    )


def _rank_semantic(request: SemanticSearch, caller: str) -> Ranking:
    """The reports most similar to the text, in similarity order; cached unless the
    embedding failed, in which case the list is empty and degraded."""
    if settings.EMBEDDINGS_MODEL is None:
        raise ValueError("A semantic search needs an embedding model (EMBEDDINGS_MODEL).")
    timeout = settings.SEARCH_RANKING_CACHE_TIMEOUT_SECONDS
    key = _cache_key(
        layer="semantic",
        text=request.text,
        filters=asdict(request.filters),
        ef_search=settings.SEARCH_HNSW_EF_SEARCH,
        iterative=settings.SEARCH_SEMANTIC_ITERATIVE_SCAN,
        embeddings=_embedding_fingerprint(),
    )
    if timeout > 0:
        cached = cache.get(key)
        if cached is not None:
            similarity = dict(zip(cached["ids"], cached["similarities"], strict=True))
            return _semantic_ranking(list(cached["ids"]), similarity, degraded=False)
    vec = _embed_query_cached(request.text, caller)
    if vec is None:
        return _semantic_ranking([], {}, degraded=True)
    filter_query = _build_filter_query(request.filters)
    # SET LOCAL lasts to the end of the enclosing transaction, so the scan gets its own:
    # the search view runs outside any transaction and the setting ends with the scan.
    with transaction.atomic():
        if settings.SEARCH_SEMANTIC_ITERATIVE_SCAN:
            with connection.cursor() as cursor:
                cursor.execute(ITERATIVE_SCAN_SQL)
        rows = list(_semantic_scan_queryset(filter_query, vec))
    similarity = _finite_similarities(rows)
    ids = list(similarity)
    if timeout > 0:
        cache.set(
            key,
            {"ids": ids, "similarities": [similarity[rid] for rid in ids]},
            timeout=timeout,
        )
    return _semantic_ranking(ids, similarity, degraded=False)


def _effective_min_similarity(request: SemanticSearch) -> float | None:
    if request.min_similarity is not None:
        return request.min_similarity
    return settings.SEARCH_SEMANTIC_MIN_SIMILARITY


def _truncate(ranking: Ranking, cutoff: float | None) -> Ranking:
    """Cut the similarity-ordered list at the first hit below ``cutoff``. Done after
    retrieval rather than as a SQL predicate: with iterative scan on, pgvector would
    keep scanning for rows that can no longer pass, up to its tuple budget."""
    if cutoff is None or cutoff <= 0:
        return ranking
    kept: list[int] = []
    for rid in ranking.ids:
        if ranking.similarity_by_id.get(rid, 0.0) < cutoff:
            break
        kept.append(rid)
    return ranking._replace(ids=kept)


def _hydration_queryset(
    page_ids: list[int],
    group: int | None,
    configs: list[tuple[str, list[str]]],
    query_str: str,
):
    """The page slice: single-table apart from the headline's deliberate join on
    ``report__body``. The group predicate keeps a report removed from the group off
    the page at once even while the cached ranking still lists it."""
    rows = ReportSearchIndex.objects.filter(report_id__in=page_ids).filter(_group_query(group))
    if not configs or not query_str:
        return rows.annotate(
            headline=Value("", output_field=TextField()),
            rank=Value(None, output_field=FloatField()),
        )
    tsqueries = {
        config: SearchQuery(query_str, search_type="raw", config=config) for config, _ in configs
    }

    def _headline(config: str) -> SearchHeadline:
        return SearchHeadline(
            "report__body",
            tsqueries[config],
            config=config,
            start_sel=HEADLINE_START,
            stop_sel=HEADLINE_STOP,
            min_words=10,
            max_words=20,
            max_fragments=10,
        )

    # Highlight and rank each document under the configuration it was indexed under;
    # a headline or rank built under another configuration silently yields nothing.
    headline_expr = Case(
        *[When(language_code__in=codes, then=_headline(config)) for config, codes in configs],
        default=Value(""),
        output_field=TextField(),
    )
    rank_expr = Case(
        *[
            When(language_code__in=codes, then=SearchRank(F("search_vector"), tsqueries[config]))
            for config, codes in configs
        ],
        default=Value(0.0),
        output_field=FloatField(),
    )
    return rows.annotate(headline=headline_expr, rank=rank_expr)


def _hydrate(page_ids: list[int], ranking: Ranking, group: int | None) -> list[ReportDocument]:
    rows = _hydration_queryset(page_ids, group, ranking.configs, ranking.query_str)
    by_id = {
        r.report.pk: cast(AnnotatedReportSearchIndex, r)
        for r in rows.select_related("report").prefetch_related("report__modalities")
    }
    documents: list[ReportDocument] = []
    for rid in page_ids:
        row = by_id.get(rid)
        if row is None:  # deleted, or removed from the group, since the ranking was cached
            continue
        documents.append(
            document_from_pgsearch_response(
                row,
                relevance=row.rank,
                summary=summary_markup(row.report.body, row.headline or ""),
                similarity=ranking.similarity_by_id.get(rid),
                score=ranking.score_by_id.get(rid, 0.0),
            )
        )
    return documents


def _page(ids: list[int], request: Search) -> list[int]:
    if request.limit is None:
        return ids[request.offset :]
    if request.limit <= 0:  # a negative slice bound would count from the end of the list
        return []
    return ids[request.offset : request.offset + request.limit]


def search(request: SearchRequest) -> SearchResult:
    if isinstance(request, SemanticSearch):
        ranking = _truncate(
            _rank_semantic(request, "Semantic search"), _effective_min_similarity(request)
        )
    else:
        ranking = _rank_fulltext(request)
    documents = _hydrate(_page(ranking.ids, request), ranking, request.filters.group)
    return SearchResult(
        total_count=len(ranking.ids),
        total_relation=ranking.relation,
        documents=documents,
        degraded=ranking.degraded,
    )


def count(request: FullTextSearch) -> int:
    return len(_keyword_ranking(request).ids)


def retrieve(request: FullTextSearch) -> Iterator[str]:
    ordered_ids = _keyword_ranking(request).ids
    if not ordered_ids:
        return iter([])
    # The cached ids may predate a group change; as on the search page, the group
    # predicate keeps a report removed from the group out at once.
    id_to_doc = dict(
        ReportSearchIndex.objects.filter(report_id__in=ordered_ids)
        .filter(_group_query(request.filters.group))
        .values_list("report_id", "report__document_id")
    )
    return (id_to_doc[rid] for rid in ordered_ids if rid in id_to_doc)


def filter(filter: SearchFilters) -> Iterator[str]:
    filter_query = _build_filter_query(filter)
    results = ReportSearchIndex.objects.filter(filter_query).values_list(
        "report__document_id", flat=True
    )
    return results.iterator()
