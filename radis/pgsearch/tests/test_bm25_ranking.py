import pytest
from django.contrib.auth.models import Group
from django.core.cache import cache
from django.core.management import call_command
from django.db import connection

from radis.pgsearch.apps import check_bm25_ranking_prerequisites
from radis.pgsearch.providers import search
from radis.pgsearch.utils.bm25_utils import bm25_index_name
from radis.reports.factories import ReportFactory
from radis.reports.models import Report
from radis.search.site import Search, SearchFilters
from radis.search.utils.query_parser import QueryParser

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _requires_pg_textsearch(db):
    """These tests need a postgres that ships pg_textsearch (see
    docker/postgres/Dockerfile); on a stock pgvector image they skip rather
    than fail, so the default CI database keeps passing."""
    with connection.cursor() as cursor:
        cursor.execute("SELECT 1 FROM pg_available_extensions WHERE name = 'pg_textsearch'")
        if cursor.fetchone() is None:
            pytest.skip("pg_textsearch is not available in this PostgreSQL installation")


@pytest.fixture(autouse=True)
def _bm25_mode(settings):
    settings.HYBRID_FTS_RANKING = "bm25"
    # Isolate the FTS ordering: with no embedding model the fused order is the
    # FTS order, so assertions test BM25 ranking and nothing else.
    settings.EMBEDDINGS_MODEL = None
    cache.clear()
    yield
    cache.clear()


def _make_report(body: str) -> Report:
    return ReportFactory.create(body=body, language__code="en")


def _make_search(query_str: str, group_id: int) -> Search:
    node, _ = QueryParser().parse(query_str)
    assert node is not None
    return Search(
        query=node,
        filters=SearchFilters(group=group_id, language="en"),
        offset=0,
        limit=25,
    )


@pytest.fixture
def group(db):
    return Group.objects.create(name="radiology")


def test_sync_bm25_indexes_creates_extension_and_per_language_indexes(group):
    _make_report(body="pneumothorax")  # creates the Language row

    call_command("sync_bm25_indexes")

    with connection.cursor() as cursor:
        cursor.execute("SELECT 1 FROM pg_extension WHERE extname = 'pg_textsearch'")
        assert cursor.fetchone() is not None
        cursor.execute(
            "SELECT 1 FROM pg_indexes WHERE indexname = %s", [bm25_index_name("en")]
        )
        assert cursor.fetchone() is not None

    # Idempotent: a second run must not fail on the existing index.
    call_command("sync_bm25_indexes")


def test_bm25_ranks_by_term_frequency(group):
    r_once = _make_report(body="A single pneumothorax mention in a longer report body.")
    r_many = _make_report(body="Pneumothorax confirmed. Pneumothorax unchanged. Pneumothorax.")
    r_none = _make_report(body="Lungs are clear bilaterally.")
    for r in (r_once, r_many, r_none):
        r.groups.add(group)
    call_command("sync_bm25_indexes")

    result = search(_make_search("pneumothorax", group.pk))

    ids = [d.document_id for d in result.documents]
    assert ids == [r_many.document_id, r_once.document_id]
    assert r_none.document_id not in ids


def test_boolean_semantics_still_gate_membership(group):
    r_plain = _make_report(body="Pneumothorax on the left side.")
    r_negated = _make_report(body="Pneumothorax with drainage in place.")
    for r in (r_plain, r_negated):
        r.groups.add(group)
    call_command("sync_bm25_indexes")

    result = search(_make_search("pneumothorax AND NOT drainage", group.pk))

    ids = [d.document_id for d in result.documents]
    assert ids == [r_plain.document_id]


def test_database_check_reports_missing_indexes_then_passes(group):
    _make_report(body="pneumothorax")
    with connection.cursor() as cursor:
        cursor.execute("CREATE EXTENSION IF NOT EXISTS pg_textsearch")

    errors = check_bm25_ranking_prerequisites(None, databases=["default"])
    assert [e.id for e in errors] == ["pgsearch.E005"]

    call_command("sync_bm25_indexes")
    assert check_bm25_ranking_prerequisites(None, databases=["default"]) == []
