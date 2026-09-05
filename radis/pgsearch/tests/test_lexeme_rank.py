import pytest
from django.contrib.auth.models import Group
from django.core.management import call_command
from django.db import connection
from django.test.utils import CaptureQueriesContext

from radis.pgsearch.models import LexemeRank
from radis.pgsearch.providers import search
from radis.pgsearch.utils import lexeme_rank
from radis.reports.factories import ReportFactory
from radis.reports.models import Report
from radis.search.site import Search, SearchFilters
from radis.search.utils.query_parser import QueryParser

pytestmark = pytest.mark.django_db

# The ORM always renders the table reference this way; matching the bare table
# name instead would also hit the trigger-name literal inside the readiness
# probe's pg_trigger query.
LEXEME_TABLE_SQL = 'FROM "pgsearch_lexemerank"'


@pytest.fixture(autouse=True)
def _fts_only(settings):
    """These tests are about the FTS arm; a configured embedding model (e.g. a
    developer's .env leaking in) would add live embed attempts to every search."""
    settings.EMBEDDINGS_MODEL = None


@pytest.fixture(autouse=True)
def _fresh_ready_probe():
    """The trigger probe memoizes per process while triggers come and go with
    each test's transaction, so every test must probe anew."""
    lexeme_rank.reset_ready_cache()
    yield
    lexeme_rank.reset_ready_cache()


@pytest.fixture
def group(db):
    return Group.objects.create(name="radiology")


def _make_report(body: str, language_code: str = "en") -> Report:
    report = ReportFactory.create(body=body, language__code=language_code)
    return report


def _make_search(query_str: str, group_id: int, language: str | None = "en") -> Search:
    node, _ = QueryParser().parse(query_str)
    assert node is not None
    return Search(
        query=node,
        filters=SearchFilters(group=group_id, language=language)
        if language
        else SearchFilters(group=group_id),
        offset=0,
        limit=25,
    )


@pytest.fixture
def corpus(group):
    """Three matches with distinct term frequencies plus one non-match, so the
    expected ranking is unambiguous."""
    bodies = [
        "A single pneumothorax mention in a longer report body with many other findings.",
        "Pneumothorax confirmed. Pneumothorax unchanged. Pneumothorax stable today.",
        "Pneumothorax noted twice: small pneumothorax apically.",
        "Lungs are clear bilaterally.",
    ]
    reports = []
    for body in bodies:
        report = _make_report(body)
        report.groups.add(group)
        reports.append(report)
    return reports


def _document_ids(result):
    return [doc.document_id for doc in result.documents]


def test_backfill_then_single_term_matches_ts_rank_exactly(corpus, group, settings):
    settings.HYBRID_FTS_LEXEME_RANK_INDEX = True
    call_command("sync_lexeme_ranks")

    with CaptureQueriesContext(connection) as ctx:
        fast = search(_make_search("pneumothorax", group.pk))
    assert any(LEXEME_TABLE_SQL in q["sql"] for q in ctx.captured_queries)

    settings.HYBRID_FTS_LEXEME_RANK_INDEX = False
    with CaptureQueriesContext(connection) as ctx:
        slow = search(_make_search("pneumothorax", group.pk))
    assert not any(LEXEME_TABLE_SQL in q["sql"] for q in ctx.captured_queries)

    # Same members, same order, same count -- the fast path must be a pure
    # execution-plan change, never a ranking change.
    assert _document_ids(fast) == _document_ids(slow)
    assert fast.total_count == slow.total_count == 3


def test_stored_rank_equals_ts_rank_value(corpus, group, settings):
    settings.HYBRID_FTS_LEXEME_RANK_INDEX = True
    call_command("sync_lexeme_ranks")

    from django.contrib.postgres.search import SearchQuery, SearchRank
    from django.db.models import F

    from radis.pgsearch.models import ReportSearchIndex

    report = corpus[1]
    tsquery = SearchQuery("'pneumothorax'", search_type="raw", config="english")
    annotated = (
        ReportSearchIndex.objects.filter(report=report)
        .annotate(ts=SearchRank(F("search_vector"), tsquery))
        .values_list("ts", flat=True)
        .get()
    )
    stored = LexemeRank.objects.get(report=report, lexeme="pneumothorax").rank
    assert stored == pytest.approx(annotated, rel=1e-6)


def test_multi_term_query_keeps_ts_rank_path(corpus, group, settings):
    settings.HYBRID_FTS_LEXEME_RANK_INDEX = True
    call_command("sync_lexeme_ranks")

    with CaptureQueriesContext(connection) as ctx:
        result = search(_make_search("pneumothorax stable", group.pk))
    assert not any(LEXEME_TABLE_SQL in q["sql"] for q in ctx.captured_queries)
    assert result.total_count == 1


def test_phrase_query_keeps_ts_rank_path(corpus, group, settings):
    settings.HYBRID_FTS_LEXEME_RANK_INDEX = True
    call_command("sync_lexeme_ranks")

    with CaptureQueriesContext(connection) as ctx:
        search(_make_search('"pneumothorax confirmed"', group.pk))
    assert not any(LEXEME_TABLE_SQL in q["sql"] for q in ctx.captured_queries)


def test_trigger_indexes_new_and_updated_reports(group, settings):
    settings.HYBRID_FTS_LEXEME_RANK_INDEX = True
    call_command("sync_lexeme_ranks")

    report = _make_report("Pleural effusion on the right side.")
    report.groups.add(group)
    assert LexemeRank.objects.filter(report=report, lexeme="effus").exists()

    report.body = "Complete resolution, no effusion remains, small atelectasis."
    report.save()
    lexemes = set(LexemeRank.objects.filter(report=report).values_list("lexeme", flat=True))
    assert "atelectasi" in lexemes
    assert "pleural" not in lexemes


def test_no_language_filter_searches_all_configs(group, settings):
    en = _make_report("Pneumothorax on the left.", language_code="en")
    de = _make_report("Ausgedehnter Pneumothorax links.", language_code="de")
    for report in (en, de):
        report.groups.add(group)

    settings.HYBRID_FTS_LEXEME_RANK_INDEX = True
    call_command("sync_lexeme_ranks")

    fast = search(_make_search("pneumothorax", group.pk, language=None))
    settings.HYBRID_FTS_LEXEME_RANK_INDEX = False
    slow = search(_make_search("pneumothorax", group.pk, language=None))
    assert sorted(_document_ids(fast)) == sorted(_document_ids(slow))
    assert fast.total_count == slow.total_count == 2
    assert _document_ids(fast) == _document_ids(slow)


def test_stopword_matches_nothing_like_the_fallback(corpus, group, settings):
    settings.HYBRID_FTS_LEXEME_RANK_INDEX = True
    call_command("sync_lexeme_ranks")

    fast = search(_make_search("the", group.pk))
    settings.HYBRID_FTS_LEXEME_RANK_INDEX = False
    slow = search(_make_search("the", group.pk))
    assert fast.total_count == slow.total_count == 0


def test_missing_trigger_falls_back_with_warning(corpus, group, settings, caplog):
    settings.HYBRID_FTS_LEXEME_RANK_INDEX = True
    # sync_lexeme_ranks deliberately not run.

    with caplog.at_level("WARNING", logger="radis.pgsearch.utils.lexeme_rank"):
        with CaptureQueriesContext(connection) as ctx:
            result = search(_make_search("pneumothorax", group.pk))

    assert result.total_count == 3
    assert not any(LEXEME_TABLE_SQL in q["sql"] for q in ctx.captured_queries)
    assert any("sync_lexeme_ranks" in record.message for record in caplog.records)


def test_flag_off_is_the_default_and_touches_nothing(corpus, group, settings):
    assert settings.HYBRID_FTS_LEXEME_RANK_INDEX is False
    with CaptureQueriesContext(connection) as ctx:
        result = search(_make_search("pneumothorax", group.pk))
    assert result.total_count == 3
    assert not any(LEXEME_TABLE_SQL in q["sql"] for q in ctx.captured_queries)
    assert not any("pg_trigger" in q["sql"] for q in ctx.captured_queries)


def test_remove_undoes_trigger_and_rows(corpus, group, settings):
    settings.HYBRID_FTS_LEXEME_RANK_INDEX = True
    call_command("sync_lexeme_ranks")
    assert LexemeRank.objects.exists()

    call_command("sync_lexeme_ranks", "--remove")
    assert not LexemeRank.objects.exists()
    with connection.cursor() as cursor:
        assert not lexeme_rank.trigger_installed(cursor)
