import hashlib
import json
import logging
import unicodedata
from collections.abc import Iterator
from datetime import date, datetime, time, timedelta
from typing import Literal, NamedTuple, cast

import openai
from django.conf import settings
from django.contrib.postgres.search import SearchHeadline, SearchQuery, SearchRank
from django.core.cache import cache
from django.db.models import Case, F, FloatField, Q, TextField, Value, When
from django.utils import timezone
from pgvector.django import CosineDistance

from radis.core.utils.embedding_client import (
    PERMANENT_EMBEDDING_ERRORS,
    EmbeddingClient,
    EmbeddingClientError,
)
from radis.core.utils.rate_limit import RateLimited
from radis.reports.models import Language, Report
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
from .utils.fusion import rrf_fuse, summary_with_fallback
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


def _collect_and_negations(node: QueryNode) -> list[QueryNode]:
    """Operands of every ``NOT`` reachable from the root through ``AND``/parens
    only.

    These negations constrain the whole result set, so they can be enforced on
    the vector candidates as well as the FTS side. A ``NOT`` under an ``OR`` is
    deliberately skipped: its exclusion is branch-scoped (in ``(A AND NOT B) OR
    C`` a doc matching ``C`` may legitimately contain ``B``), so excluding it
    globally would drop valid hits. That residual is the documented limitation
    in docs/superpowers/specs/hybrid-search.md §7.8 / §11.5, and matches how
    Elasticsearch/Weaviate apply a single global ``must_not`` filter."""
    if isinstance(node, UnaryNode):
        assert node.operator == "NOT"
        return [node.operand]
    if isinstance(node, ParensNode):
        return _collect_and_negations(node.expression)
    if isinstance(node, BinaryNode) and node.operator == "AND":
        return _collect_and_negations(node.left) + _collect_and_negations(node.right)
    return []


def _build_negative_query_string(node: QueryNode) -> str:
    """Raw-tsquery string matching any document the top-level ``NOT`` branches
    exclude, or ``""`` when there are none.

    The vector half embeds only the positive branches (§7.8 strips ``NOT``), so
    without this a document that is semantically near the positive concept but
    contains a negated term re-enters through the RRF union even though the FTS
    half rejects it. Applying this as ``.exclude(search_vector=...)`` on the
    vector queryset closes that leak. Only the negation is enforced — never the
    positive branches — so the vector half keeps surfacing semantic matches
    that don't lexically contain the query terms."""
    parts = [_build_query_string(operand) for operand in _collect_and_negations(node)]
    parts = [part for part in parts if part]
    # A document matching ANY negated branch must be excluded.
    return " | ".join(f"({part})" for part in parts)


def _exclude_negations(queryset, node: QueryNode, configs: list[tuple[str, list[str]]]):
    """Drop documents matching the query's top-level ``NOT`` branches from a
    vector-candidate queryset, so the FTS half's exclusions also bind the vector
    half. Each configuration is excluded separately: a document is only dropped
    when it matches the negation under the configuration it was indexed with, so
    the exclusion cannot silently no-op the way one shared configuration did."""
    negative_query_str = _build_negative_query_string(node)
    if not negative_query_str:
        return queryset
    for config, codes in configs:
        negative_tsquery = SearchQuery(negative_query_str, search_type="raw", config=config)
        queryset = queryset.exclude(Q(language_code__in=codes) & Q(search_vector=negative_tsquery))
    return queryset


def _local_day_start(day: date) -> datetime:
    """Midnight of ``day`` in the active timezone, as an aware datetime."""
    return timezone.make_aware(datetime.combine(day, time.min))


def _build_filter_query(filters: SearchFilters) -> Q:
    # Every predicate here is a column on ReportSearchIndex itself -- the search
    # projection (see migration 0003) mirrors the Report fields search filters
    # on, so the candidate query stays single-table. Reintroducing a ``report__``
    # traversal restores the join and the multi-second query shape it caused;
    # test_filter_query_plan_is_single_table guards against that.
    #
    # Group-scoped access control. ``SearchView`` supplies
    # ``group=active_group.pk``; the extraction preview may pass ``group=None``
    # when the user has no active group, which is fail-closed: an empty
    # ``group_ids`` matches only reports assigned to no group at all.
    if filters.group is None:
        fq = Q(group_ids=[])
    else:
        fq = Q(group_ids__contains=[filters.group])

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
    """Embed the query text, or return None to signal FTS-only fallback.

    Search must stay usable when the embedding service doesn't, so every
    failure falls back — but at different log levels: transient conditions
    (rate limiting, connection blips, 5xx) are expected under load and log a
    WARNING, while permanent misconfiguration logs a full exception (once per
    configuration, see _LOGGED_PERMANENT_FAILURE_CONFIGS) so it reaches operators
    instead of hiding as a silently degraded search."""
    try:
        with EmbeddingClient() as ec:
            return ec.embed_query(query_text)
    except (EmbeddingClientError, *PERMANENT_EMBEDDING_ERRORS) as exc:
        config_key = _embedding_config_key()
        if config_key not in _LOGGED_PERMANENT_FAILURE_CONFIGS:
            _LOGGED_PERMANENT_FAILURE_CONFIGS.add(config_key)
            logger.exception(
                "%s falling back to FTS-only; embedding service looks misconfigured", caller
            )
        else:
            base_url, model = config_key
            # Include the concrete exception type/message: the suppressed traceback that
            # would otherwise explain *what* changed (wrong path vs. expired key vs. bad
            # request) is long gone from recent logs by the time this branch fires again.
            logger.warning(
                "%s falling back to FTS-only; embedding service still misconfigured for "
                "this configuration (check EMBEDDINGS_BASE_URL=%r and EMBEDDINGS_MODEL=%r): "
                "%s: %s -- search is serving full-text-only results until this is fixed; "
                "the full traceback was already logged once for this configuration",
                caller,
                base_url,
                model,
                type(exc).__name__,
                exc,
            )
        return None
    except (RateLimited, openai.OpenAIError) as e:
        logger.warning("%s falling back to FTS-only: %s", caller, e)
        return None


def _embed_query_cached(query_text: str, caller: str) -> list[float] | None:
    """Cache wrapper around `_embed_query_or_none`.

    Pagination re-runs the whole search for every page, so without this every
    page load re-calls the embedding service for the same query text. The key
    covers everything that determines the vector — endpoint, model, spec
    parameters, instruction, dim, query text — because a shared cache backend
    (production uses the database) outlives process restarts and thus config
    changes: "qwen3" on one deployment and "qwen3" on another are different
    weights, so the endpoint has to be part of the fingerprint too (the API key
    is not identity and stays out of the cache key). Failures are not cached: a
    transient outage must not pin searches to FTS-only for the TTL.
    """
    spec = settings.EMBEDDINGS_MODEL
    if spec is None:
        # Nothing to embed against. Task 3 adds the caller-side guard so this helper is
        # not even reached in an FTS-only deployment; returning None keeps it correct on
        # its own in the meantime, and afterwards for any future caller.
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


class _FusedHybrid(NamedTuple):
    ordered_ids: list[int]
    rrf_score_by_id: dict[int, float]
    vec_distance: dict[int, float]
    total_relation: Literal["exact", "at_least", "approximately"]
    configs: list[tuple[str, list[str]]]
    query_str: str


def _fuse_hybrid(search: Search, caller: str) -> _FusedHybrid:
    """Run both retrievers and fuse them with RRF.

    Shared by search(), retrieve() and count() so all three describe the same
    union. The vector half embeds only the positive branches (§7.8 strips
    ``NOT``) and enforces the top-level negations on its candidates; the FTS
    half consumes the full boolean tsquery. Returns the fused, ordered report
    ids plus the per-id scores/distances and the query metadata callers reuse."""
    query_str = _build_query_string(search.query)
    configs = _language_configs(search.filters)
    filter_query = _build_filter_query(search.filters)
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

    # Vector side: skipped entirely when no embedding model is configured (FTS-only
    # deployment), and when stripping NOT branches leaves nothing to embed (see
    # docs/superpowers/specs/hybrid-search.md §7.8).
    query_text = QueryParser.unparse_for_embedding(search.query)
    query_vec: list[float] | None = None
    if settings.EMBEDDINGS_MODEL is not None and query_text.strip():
        query_vec = _embed_query_cached(query_text, caller)

    vec_rank: dict[int, int] = {}
    vec_distance: dict[int, float] = {}
    if query_vec is not None:
        vec_qs = ReportSearchIndex.objects.filter(filter_query)
        vec_qs = _exclude_negations(vec_qs, search.query, configs)
        vec_rows = list(
            vec_qs.exclude(embedding__isnull=True)
            .annotate(distance=CosineDistance("embedding", query_vec))
            .order_by("distance", "report_id")
            .values_list("report_id", "distance")[: settings.HYBRID_VECTOR_TOP_K]
        )
        for i, (rid, dist) in enumerate(vec_rows):
            vec_rank[rid] = i + 1
            vec_distance[rid] = float(dist)

    # FTS side: bounded set, ts_rank only (no headline at this stage). An empty
    # ``configs`` means an empty corpus (Report.language is a required FK), and
    # an empty match_q would match every row rather than none, so skip it.
    fts_rows = (
        []
        if not configs
        else list(
            ReportSearchIndex.objects.filter(filter_query)
            .filter(match_q)
            .annotate(rank=rank_expr)
            .order_by("-rank", "report_id")
            .values("report_id", "rank")[: settings.HYBRID_FTS_MAX_RESULTS]
        )
    )
    fts_rank = {row["report_id"]: i + 1 for i, row in enumerate(fts_rows)}

    # Fusion.
    ordered_pairs = rrf_fuse(vec_rank, fts_rank, k=settings.HYBRID_RRF_K)
    rrf_score_by_id = dict(ordered_pairs)
    ordered_ids = list(rrf_score_by_id)
    total_relation: Literal["exact", "at_least", "approximately"] = (
        "at_least"
        if (
            len(fts_rows) >= settings.HYBRID_FTS_MAX_RESULTS
            or len(vec_rank) >= settings.HYBRID_VECTOR_TOP_K
        )
        else "exact"
    )
    return _FusedHybrid(
        ordered_ids=ordered_ids,
        rrf_score_by_id=rrf_score_by_id,
        vec_distance=vec_distance,
        total_relation=total_relation,
        configs=configs,
        query_str=query_str,
    )


def search(search: Search) -> SearchResult:
    fused = _fuse_hybrid(search, "Hybrid search")
    ordered_ids = fused.ordered_ids
    vec_distance = fused.vec_distance
    rrf_score_by_id = fused.rrf_score_by_id
    total_count = len(ordered_ids)
    total_relation = fused.total_relation

    if search.limit is None:
        page_ids = ordered_ids[search.offset :]
    else:
        page_ids = ordered_ids[search.offset : search.offset + search.limit]

    configs = fused.configs
    tsqueries = {
        config: SearchQuery(fused.query_str, search_type="raw", config=config)
        for config, _ in configs
    }

    def _headline(config: str) -> SearchHeadline:
        return SearchHeadline(
            "report__body",
            tsqueries[config],
            config=config,
            start_sel="<em>",
            stop_sel="</em>",
            min_words=10,
            max_words=20,
            max_fragments=10,
        )

    # Highlight and rank each document under the configuration it was indexed
    # under; a headline/rank built under another configuration silently
    # highlights/scores nothing. A one-branch Case is legal (unlike Greatest),
    # so this covers the single-configuration case too with no special-casing.
    summary_expr = Case(
        *[When(language_code__in=codes, then=_headline(config)) for config, codes in configs],
        default=Value(""),
        output_field=TextField(),
    )
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

    # Headline + hydration for the page slice only.
    page_rows = (
        ReportSearchIndex.objects.filter(report_id__in=page_ids)
        .annotate(
            summary=summary_expr,
            rank=rank_expr,
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
            rsv.report.body,
            rsv.summary or "",  # type: ignore[attr-defined]
            max_words=30,
        )
        documents.append(
            document_from_pgsearch_response(
                cast(AnnotatedReportSearchIndex, rsv),
                cosine_distance=vec_distance.get(rid),
                rrf_score=rrf_score_by_id.get(rid, 0.0),
            )
        )

    return SearchResult(total_count=total_count, total_relation=total_relation, documents=documents)


def count(search: Search) -> int:
    # Must describe the same set retrieve() yields: the extraction max-reports
    # guard is computed from this and then retrieve() iterates the union. An
    # FTS-only count would report 0 for a semantic-only query and let the guard
    # be bypassed while retrieve() still creates extraction instances.
    return len(_fuse_hybrid(search, "Hybrid count").ordered_ids)


def retrieve(search: Search) -> Iterator[str]:
    ordered_ids = _fuse_hybrid(search, "Hybrid retrieve").ordered_ids
    if not ordered_ids:
        return iter([])

    id_to_doc = dict(Report.objects.filter(pk__in=ordered_ids).values_list("pk", "document_id"))
    return (id_to_doc[rid] for rid in ordered_ids if rid in id_to_doc)


def filter(filter: SearchFilters) -> Iterator[str]:
    filter_query = _build_filter_query(filter)
    results = ReportSearchIndex.objects.filter(filter_query).values_list(
        "report__document_id", flat=True
    )
    return results.iterator()
