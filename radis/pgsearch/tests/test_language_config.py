import pytest
from django.contrib.auth.models import Group

from radis.pgsearch.providers import _language_configs, retrieve, search
from radis.reports.factories import LanguageFactory, ReportFactory
from radis.search.site import FullTextSearch, SearchFilters
from radis.search.utils.query_parser import QueryParser

pytestmark = pytest.mark.django_db


def _make_search(query_str: str, group_id: int, language: str = "") -> FullTextSearch:
    node, _ = QueryParser().parse(query_str)
    assert node is not None
    return FullTextSearch(
        query=node,
        filters=SearchFilters(group=group_id, language=language),
        offset=0,
        limit=25,
    )


@pytest.fixture
def group(db):
    return Group.objects.create(name="radiology")


@pytest.fixture
def english_report(group):
    """A report whose language stems: 'effusion' is indexed as 'effus'."""
    report = ReportFactory.create(
        body="Findings: large pleural effusion on the left.",
        language=LanguageFactory.create(code="en"),
    )
    report.groups.add(group)
    return report


def test_a_stemmed_term_is_found_without_a_language_filter(group, english_report):
    """The bug this fixes: searching the exact word in the report found nothing,
    because the document was indexed under 'english' ('effus') while a filterless
    query was built under 'simple' ('effusion')."""
    result = search(_make_search("effusion", group.pk))

    assert [doc.document_id for doc in result.documents] == [english_report.document_id]


def test_a_negated_stemmed_term_is_excluded_without_a_language_filter(group, english_report):
    """The same mismatch made `NOT` a silent no-op: the exclusion never matched,
    so a report containing the negated term came back as a hit."""
    doc_ids = list(retrieve(_make_search("pleural AND NOT effusion", group.pk)))

    assert english_report.document_id not in doc_ids


def test_an_explicit_language_filter_still_works(group, english_report):
    """The already-correct path: filtering by language restricts the queryset AND
    picks the matching config, so both sides agree."""
    result = search(_make_search("effusion", group.pk, language="en"))

    assert [doc.document_id for doc in result.documents] == [english_report.document_id]


def test_documents_in_different_languages_are_each_matched_under_their_own_config(group):
    """A mixed corpus: each document must be matched, ranked and highlighted
    under the config it was indexed with, not under one shared config."""
    english = ReportFactory.create(
        body="Findings: large pleural effusion.",
        language=LanguageFactory.create(code="en"),
    )
    english.groups.add(group)
    german = ReportFactory.create(
        body="Befund: Pleuraergüsse beidseits.",
        language=LanguageFactory.create(code="de"),
    )
    german.groups.add(group)

    english_documents = search(_make_search("effusion", group.pk)).documents
    german_documents = search(_make_search("Pleuraerguss", group.pk)).documents
    english_hits = [doc.document_id for doc in english_documents]
    german_hits = [doc.document_id for doc in german_documents]

    assert english.document_id in english_hits
    assert german.document_id in german_hits
    # Branch scoping: to_tsvector('german', ...) / to_tsquery('english', ...)
    # (and vice versa) genuinely do not meet, so a query in one language must
    # not cross-match the other document.
    assert german.document_id not in english_hits
    assert english.document_id not in german_hits

    # Highlighting must key off the document's own configuration too: a
    # headline built under another configuration silently highlights nothing,
    # and summary_markup masks that as a plain body excerpt instead of
    # failing loudly.
    en_doc = next(d for d in english_documents if d.document_id == english.document_id)
    assert "<em>effusion</em>" in en_doc.summary
    de_doc = next(d for d in german_documents if d.document_id == german.document_id)
    assert "<em>" in de_doc.summary


def test_a_language_postgres_cannot_stem_still_matches(group):
    """Codes with no Postgres dictionary resolve to 'simple' on both sides, which
    already agreed — this must keep working after the change."""
    report = ReportFactory.create(
        body="Findings: pleural effusion.",
        language=LanguageFactory.create(code="th"),
    )
    report.groups.add(group)

    hits = [doc.document_id for doc in search(_make_search("effusion", group.pk)).documents]

    assert report.document_id in hits


def test_multiple_codes_sharing_the_simple_config_are_grouped_together(group):
    """Two languages Postgres has no dictionary for both fall back to 'simple',
    landing in the SAME branch with BOTH codes in its `codes__in` list -- the
    only path that exercises `_language_configs`'s `setdefault(...).append(...)`
    grouping with more than one code, for the grouped branch's FTS match
    (below) and for a NOT term baked into that same raw tsquery."""
    thai = ReportFactory.create(
        body="Findings: opacity in the right lung.",
        language=LanguageFactory.create(code="th"),
    )
    thai.groups.add(group)
    chinese = ReportFactory.create(
        body="Findings: opacity in the right lung, with pleural effusion.",
        language=LanguageFactory.create(code="zh"),
    )
    chinese.groups.add(group)

    configs = _language_configs(SearchFilters(group=group.pk, language=""))
    simple_codes = dict(configs)["simple"]
    assert set(simple_codes) == {"th", "zh"}

    # Matching: a filterless search for a literal 'simple'-config term finds
    # BOTH documents via the same shared branch, not just the first code
    # grouped into it.
    hits = [doc.document_id for doc in search(_make_search("opacity", group.pk)).documents]
    assert thai.document_id in hits
    assert chinese.document_id in hits

    # NOT, via the grouped branch's own raw FTS tsquery: the tsquery itself
    # embeds '!effusion', so it excludes a document under either code sharing
    # that branch.
    doc_ids = list(retrieve(_make_search("opacity AND NOT effusion", group.pk)))
    assert chinese.document_id not in doc_ids
    assert thai.document_id in doc_ids
