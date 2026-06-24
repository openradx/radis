"""DB-backed tests for the PostgreSQL full-text search provider.

These exercise the real ``QueryParser`` -> ``providers.search`` -> PostgreSQL
``to_tsquery`` path against a live database, covering:

- the search-vector population side effect of saving a ``Report`` (signals),
- plain term, phrase/proximity (``<->``) and boolean (``& | !``) operators,
- ranking order,
- empty / edge queries, and
- a hostile/malformed query fed through the *real* parser to assert the
  ``search_type="raw"`` provider does not surface an unhandled
  ``ProgrammingError`` (HTTP 500) to the caller.

All Reports are created with an explicit ``en`` language so the resolved
PostgreSQL text-search config is deterministic (``english``); the test image
(pgvector/pgvector:pg17) ships the ``english``, ``german`` and ``simple``
configs.
"""

import pytest
from adit_radis_shared.accounts.factories import GroupFactory

from radis.pgsearch import providers
from radis.pgsearch.models import ReportSearchVector
from radis.reports.factories import LanguageFactory, ReportFactory
from radis.reports.models import Report
from radis.search.site import Search, SearchFilters
from radis.search.utils.query_parser import QueryNode, QueryParser, TermNode

pytestmark = pytest.mark.django_db


def _search_group():
    """The group the provider searches under (see ``_search``).

    Search is group-scoped: ``providers._build_filter_query`` filters on
    ``report__groups=filters.group``, mirroring ``Report.objects.filter(
    groups=active_group)`` used by the report views. ``make_report`` attaches
    this group so seeded reports are visible to the search; ``_search`` passes
    its pk as ``SearchFilters.group``.
    """
    return GroupFactory.create(name="pgsearch-test-group")


def make_report(body: str, *, language_code: str = "en", **overrides) -> Report:
    """Create a ``Report`` (visible to the search group) with the given body."""
    language = LanguageFactory.create(code=language_code)
    report = ReportFactory.create(language=language, body=body, **overrides)
    report.groups.add(_search_group())
    return report


def run_search(
    raw_query: str,
    *,
    language: str = "en",
    limit: int | None = 10,
    offset: int = 0,
) -> list[str]:
    """Parse ``raw_query`` and run it through the provider, returning document_ids in order."""
    node = parse(raw_query)
    assert node is not None, f"expected a parseable query for {raw_query!r}"
    result = providers.search(_search(node, language=language, limit=limit, offset=offset))
    return [doc.document_id for doc in result.documents]


def parse(raw_query: str) -> QueryNode | None:
    node, _fixes = QueryParser().parse(raw_query)
    return node


def _search(
    node: QueryNode,
    *,
    language: str = "en",
    limit: int | None = 10,
    offset: int = 0,
) -> Search:
    return Search(
        query=node,
        filters=SearchFilters(group=_search_group().pk, language=language),
        offset=offset,
        limit=limit,
    )


# ---------------------------------------------------------------------------
# Signals: saving a Report populates the search vector.
# ---------------------------------------------------------------------------


def test_saving_report_creates_and_populates_search_vector():
    report = make_report("The patient has acute pneumonia in the left lung.")

    search_vector = ReportSearchVector.objects.get(report=report)
    assert search_vector.search_vector is not None
    # lexemes are stemmed/normalised by the english config; check a stem is present
    lexemes = str(search_vector.search_vector)
    assert "pneumonia" in lexemes
    assert "lung" in lexemes
    # english stop-words are stripped out of the tsvector
    assert "the" not in lexemes.split()


def test_updating_report_body_refreshes_search_vector():
    report = make_report("initial findings about pneumonia")

    # Sanity: matches the original term.
    assert run_search("pneumonia") == [report.document_id]

    report.body = "revised findings about fracture"
    report.save()

    # The post_save signal re-saves the search vector with the new body.
    assert run_search("fracture") == [report.document_id]
    assert run_search("pneumonia") == []


# ---------------------------------------------------------------------------
# Plain term matching.
# ---------------------------------------------------------------------------


def test_plain_term_matches_only_relevant_report():
    match = make_report("CT thorax shows pneumonia.")
    make_report("MR knee shows a meniscus tear.")

    assert run_search("pneumonia") == [match.document_id]


def test_plain_term_is_stemmed():
    report = make_report("The lungs show multiple opacities.")

    # english stemming: "opacity" stem matches "opacities"
    assert run_search("opacity") == [report.document_id]


def test_plain_term_no_match_returns_empty():
    make_report("CT thorax shows pneumonia.")

    result = providers.search(_search(parse("nonexistentterm")))  # type: ignore[arg-type]
    assert result.total_count == 0
    assert result.documents == []
    assert result.total_relation == "exact"


# ---------------------------------------------------------------------------
# Phrase / proximity (<->).
# ---------------------------------------------------------------------------


def test_phrase_requires_adjacent_terms_in_order():
    adjacent = make_report("Findings consistent with pulmonary embolism today.")
    separated = make_report("There is an embolism noted distal to the pulmonary artery.")

    matched = run_search('"pulmonary embolism"')

    assert adjacent.document_id in matched
    # The phrase operator (<->) requires the words to be adjacent and ordered,
    # so the report where the words are separated must NOT match.
    assert separated.document_id not in matched


def test_phrase_order_matters():
    report = make_report("pulmonary embolism confirmed")

    assert run_search('"pulmonary embolism"') == [report.document_id]
    # reversed order is not adjacent-in-order, so no match
    assert run_search('"embolism pulmonary"') == []


# ---------------------------------------------------------------------------
# Boolean operators: AND (&), OR (|), NOT (!).
# ---------------------------------------------------------------------------


def test_and_operator_requires_both_terms():
    both = make_report("CT thorax shows pneumonia and effusion.")
    only_one = make_report("CT thorax shows pneumonia, no effusion.".replace("effusion", "x"))

    matched = run_search("pneumonia AND effusion")

    assert both.document_id in matched
    assert only_one.document_id not in matched


def test_implicit_and_between_terms():
    both = make_report("pneumonia with effusion")
    one = make_report("pneumonia only")

    # Adjacent terms without an operator are parsed as an implicit AND.
    matched = run_search("pneumonia effusion")

    assert matched == [both.document_id]
    assert one.document_id not in matched


def test_or_operator_matches_either_term():
    a = make_report("isolated pneumonia")
    b = make_report("isolated fracture")
    neither = make_report("normal study")

    matched = set(run_search("pneumonia OR fracture"))

    assert matched == {a.document_id, b.document_id}
    assert neither.document_id not in matched


def test_not_operator_excludes_term():
    with_effusion = make_report("pneumonia with effusion")
    without_effusion = make_report("pneumonia without complication")

    matched = run_search("pneumonia AND NOT effusion")

    assert matched == [without_effusion.document_id]
    assert with_effusion.document_id not in matched


def test_grouping_with_parentheses():
    target = make_report("CT thorax with pneumonia and effusion")
    other = make_report("CT thorax with fracture and effusion")
    excluded = make_report("CT thorax with pneumonia, no effusion".replace("effusion", "x"))

    matched = set(run_search("(pneumonia OR fracture) AND effusion"))

    assert matched == {target.document_id, other.document_id}
    assert excluded.document_id not in matched


# ---------------------------------------------------------------------------
# Ranking order.
# ---------------------------------------------------------------------------


def test_results_ranked_by_relevance_descending():
    # More occurrences of the query term -> higher ts_rank.
    high = make_report("pneumonia pneumonia pneumonia pneumonia bilateral pneumonia")
    low = make_report("a single mention of pneumonia among other unrelated findings here")

    ordered = run_search("pneumonia")

    assert ordered == [high.document_id, low.document_id]


def test_ranking_reflects_query_specificity():
    # Report matching both OR-terms should outrank one matching a single term.
    both = make_report("pneumonia and fracture both clearly present")
    single = make_report("only pneumonia present")

    ordered = run_search("pneumonia OR fracture")

    assert ordered[0] == both.document_id
    assert set(ordered) == {both.document_id, single.document_id}


# ---------------------------------------------------------------------------
# Pagination / count semantics.
# ---------------------------------------------------------------------------


def test_total_count_independent_of_limit():
    for i in range(5):
        make_report(f"pneumonia case number {i}")

    result = providers.search(_search(parse("pneumonia"), limit=2))  # type: ignore[arg-type]

    assert result.total_count == 5
    assert len(result.documents) == 2


def test_offset_skips_leading_results():
    reports = [make_report("pneumonia case") for _ in range(3)]
    document_ids = {r.document_id for r in reports}

    full = run_search("pneumonia", limit=None)
    skipped = run_search("pneumonia", limit=None, offset=1)

    assert set(full) == document_ids
    assert skipped == full[1:]


def test_count_helper_matches_search_total():
    for i in range(3):
        make_report(f"pneumonia variant {i}")
    make_report("unrelated normal study")

    assert providers.count(_search(parse("pneumonia"))) == 3  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Empty / edge queries.
# ---------------------------------------------------------------------------


def test_empty_query_parses_to_none():
    # A blank (or fully-stripped) query yields no AST; provider must not be called.
    assert parse("") is None
    assert parse("   ") is None
    # A query consisting solely of stripped/invalid characters also collapses.
    assert parse("***") is None


def test_query_with_only_stopwords_matches_nothing():
    make_report("pneumonia present")

    # "the" / "and" are english stop-words; the tsquery becomes empty and
    # therefore matches no documents (but must not error).
    result = providers.search(_search(parse("the and")))  # type: ignore[arg-type]
    assert result.total_count == 0


# ---------------------------------------------------------------------------
# Security: hostile / malformed query strings.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw_query",
    [
        "pneumonia & | lung",  # dangling boolean operators
        "& lung",  # leading operator
        "pneumonia <->",  # dangling proximity
        "((( unbalanced pneumonia",  # unbalanced parens
        "a:* & !",  # tsquery weight/prefix syntax
        "!!!",  # only negations
        "pneumonia; DROP TABLE reports;--",  # SQL-injection shaped
    ],
)
def test_hostile_query_via_parser_does_not_raise(raw_query):
    """Malformed input routed through the real parser must not surface a 500.

    The ``QueryParser`` sanitises tsquery meta-characters (``& | ! < > -`` are
    not "search token chars") and cleans dangling operators, so these inputs
    reach the provider as benign ASTs. This is the primary defence for the
    ``search_type="raw"`` call and must hold.
    """
    make_report("The patient has pneumonia.")

    node = parse(raw_query)
    if node is None:
        # Fully sanitised away -> nothing to search, trivially safe.
        return

    # Must not raise ProgrammingError / any DB syntax error.
    result = providers.search(_search(node))
    assert result.total_relation == "exact"


@pytest.mark.parametrize("raw_query", ["'", "''", "' OR '1'='1"])
def test_lone_apostrophe_query_does_not_raise(raw_query):
    """A token made only of apostrophes must not crash the raw-tsquery path.

    The apostrophe is in SAFE_TERM_CHARS, so such a token survives parsing, but
    ``_build_query_string`` now drops any token that carries no letter/digit/mark
    before it reaches ``to_tsquery(..., 'raw')``. The query therefore resolves to
    a benign empty/no-match search instead of a ``ProgrammingError`` (HTTP 500).
    """
    make_report("The patient has pneumonia.")

    node = parse(raw_query)
    assert node is not None  # parser keeps apostrophes as term chars

    # Must not raise ProgrammingError; the apostrophe-only token contributes no
    # lexeme, so nothing matches the seeded report.
    result = providers.search(_search(node))
    assert result.total_relation == "exact"
    assert result.documents == []


def test_hostile_raw_termnode_reaches_db_without_crashing_when_benign():
    """Directly crafted AST with benign content still flows to the DB safely.

    This bypasses the parser to confirm the provider itself executes a single
    real ``to_tsquery`` for a normal TermNode (the building block the security
    cases rely on).
    """
    report = make_report("pneumonia confirmed")

    node = TermNode("WORD", "pneumonia")
    result = providers.search(_search(node))

    assert [doc.document_id for doc in result.documents] == [report.document_id]
