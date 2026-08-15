from unittest.mock import patch

import pytest
from django.contrib.auth.models import Group

from radis.core.utils.model_spec import parse_model_spec
from radis.pgsearch.models import ReportSearchIndex
from radis.pgsearch.providers import _language_configs, retrieve, search
from radis.reports.factories import LanguageFactory, ReportFactory
from radis.search.site import Search, SearchFilters
from radis.search.utils.query_parser import QueryParser

pytestmark = pytest.mark.django_db


def _make_search(query_str: str, group_id: int, language: str = "") -> Search:
    node, _ = QueryParser().parse(query_str)
    assert node is not None
    return Search(
        query=node,
        filters=SearchFilters(group=group_id, language=language),
        offset=0,
        limit=25,
    )


def _unit_vec(idx: int, dim: int) -> list[float]:
    """Deterministic unit vector that points in dimension `idx`."""
    v = [0.0] * dim
    v[idx % dim] = 1.0
    return v


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
    # and summary_with_fallback masks that as a plain body excerpt instead of
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
    (below) and for a NOT term baked into that same raw tsquery. This does NOT
    exercise `_exclude_negations` -- EMBEDDINGS_MODEL is unset in this test, so
    the vector side (and `_exclude_negations` with it) never runs; see
    `test_negation_excludes_leaking_vector_candidates_under_each_documents_own_config`
    for the test that does reach it, with two configs."""
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

    # NOT, via the grouped branch's own raw FTS tsquery (not
    # `_exclude_negations`, the separate vector-side path this test never
    # reaches): the tsquery itself embeds '!effusion', so it excludes a
    # document under either code sharing that branch.
    doc_ids = list(retrieve(_make_search("opacity AND NOT effusion", group.pk)))
    assert chinese.document_id not in doc_ids
    assert thai.document_id in doc_ids


def test_negation_excludes_leaking_vector_candidates_under_each_documents_own_config(
    group, settings
):
    """The vector-side negation exclusion (`_exclude_negations`) is exercised
    once per configuration present in the corpus. Test settings default
    EMBEDDINGS_MODEL to None (FTS-only), which would make `_embed_query_cached`
    short-circuit before ever reaching the vector side -- configuring it here
    is required for this test to exercise that path at all (mirrors the
    autouse fixture in test_provider_hybrid.py).

    With two languages in play, a report that would otherwise leak in via
    vector similarity must be excluded under ITS OWN configuration: if the
    (config, codes) pairing were ever crossed between loop iterations, the
    wrong tsquery would be compared against the document's search_vector, the
    two would not match (own-config lexemes only), and the leak would survive.
    """
    settings.EMBEDDINGS_MODEL = parse_model_spec("qwen3")
    dim = settings.EMBEDDINGS_DIM

    body = "Findings: pneumothorax with a large pleural effusion."
    # Indexed under 'english': to_tsvector stems 'effusion' -> 'effus'.
    en_leak = ReportFactory.create(body=body, language=LanguageFactory.create(code="en"))
    en_leak.groups.add(group)
    ReportSearchIndex.objects.filter(report=en_leak).update(embedding=_unit_vec(0, dim))
    # Same text, indexed under 'german': German's stemmer leaves the
    # non-German word 'effusion' as the literal lexeme 'effusion'.
    de_leak = ReportFactory.create(body=body, language=LanguageFactory.create(code="de"))
    de_leak.groups.add(group)
    ReportSearchIndex.objects.filter(report=de_leak).update(embedding=_unit_vec(0, dim))
    # Legitimate hit: contains the positive term but not the negated one.
    good = ReportFactory.create(
        body="Findings: pneumothorax, otherwise unremarkable.",
        language=LanguageFactory.create(code="en"),
    )
    good.groups.add(group)
    ReportSearchIndex.objects.filter(report=good).update(embedding=_unit_vec(1, dim))

    with patch("radis.pgsearch.providers.EmbeddingClient") as MockClient:
        MockClient.return_value.__enter__.return_value = MockClient.return_value
        MockClient.return_value.__exit__.return_value = None
        MockClient.return_value.embed_query.return_value = _unit_vec(0, dim)
        doc_ids = list(retrieve(_make_search("pneumothorax AND NOT effusion", group.pk)))

    assert en_leak.document_id not in doc_ids
    assert de_leak.document_id not in doc_ids
    assert good.document_id in doc_ids
