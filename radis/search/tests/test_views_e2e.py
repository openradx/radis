"""End-to-end search through the *real* pgsearch provider (un-mocked).

The sibling ``test_views.py`` patches ``radis.search.views.search_provider`` with
a stub, so it never touches Postgres full-text search. These tests instead insert
real ``Report`` rows (whose ``ReportSearchVector`` is populated by the pgsearch
``post_save`` signal) and drive the whole stack:

    HTTP GET /search/  ->  SearchForm  ->  QueryParser  ->  pgsearch.providers.search
    ->  to_tsquery  ->  ReportSearchVector  ->  rendered results

so we assert the seeded report is actually found and that the form's hard
filters (modality, patient sex, age, study date, study description) propagate
into the SQL and narrow the result set.

Run with the test settings so the debug toolbar (which otherwise breaks the
rendered view) is disabled::

    DJANGO_SETTINGS_MODULE=radis.settings.test uv run pytest \
        radis/search/tests/test_views_e2e.py
"""

from datetime import UTC, datetime

import pytest
from adit_radis_shared.accounts.factories import GroupFactory, UserFactory
from django.contrib.auth.models import Group
from django.test import Client

from radis.reports.factories import LanguageFactory, ReportFactory
from radis.reports.models import Report
from radis.search.site import search_provider

pytestmark = pytest.mark.django_db


def _user_with_active_group():
    user = UserFactory.create(is_active=True)
    group = GroupFactory.create()
    user.groups.add(group)
    user.active_group = group
    user.save()
    return user, group


def _logged_in_client() -> tuple[Client, Group]:
    client = Client()
    user, group = _user_with_active_group()
    client.force_login(user)
    return client, group


def _seed_report(
    body: str, *, document_id: str, groups: list[Group] | None = None, **overrides
) -> Report:
    """Create a Report (and, via signal, its search vector) with English config.

    ``ReportFactory`` does not assign any groups by default, but search is now
    group-scoped (the provider filters on the active group). Pass ``groups`` to
    associate the report with the searching user's group so it is visible;
    reports seeded without a group (or with a foreign group) are intentionally
    invisible to that user.
    """
    language = LanguageFactory.create(code="en")
    report = ReportFactory.create(
        document_id=document_id,
        language=language,
        body=body,
        **overrides,
    )
    if groups is not None:
        report.groups.set(groups)
    return report


def _doc_ids(response) -> list[str]:
    return [doc.document_id for doc in response.context["documents"]]


def test_real_provider_is_registered():
    """Guard: the un-mocked default provider must be the pgsearch one, otherwise
    these tests would silently exercise nothing meaningful."""
    assert search_provider is not None
    assert search_provider.name == "PG Search"


def test_search_finds_real_report_through_pgsearch():
    client, group = _logged_in_client()
    match = _seed_report(
        "CT thorax demonstrates acute pneumonia in the left lower lobe.",
        document_id="E2E-MATCH",
        modalities=["CT"],
        groups=[group],
    )
    _seed_report(
        "MR of the knee shows a meniscus tear, no acute findings.",
        document_id="E2E-NOMATCH",
        modalities=["MR"],
        groups=[group],
    )

    response = client.get("/search/", {"query": "pneumonia"})

    assert response.status_code == 200
    ids = _doc_ids(response)
    assert match.document_id in ids
    assert "E2E-NOMATCH" not in ids
    assert response.context["total_count"] == 1
    assert response.context["total_relation"] == "exact"


def test_search_stemming_through_full_stack():
    """With the language filter set to 'en' the query config matches the document
    config (both english), so stemming applies and 'opacity' matches 'opacities'.

    The ``language=en`` param is load-bearing: see
    ``test_language_config_mismatch_disables_stemming`` for what happens without
    it (the query defaults to the 'simple' config and stemming is lost).
    """
    client, group = _logged_in_client()
    report = _seed_report(
        "The lungs show multiple opacities bilaterally.",
        document_id="E2E-STEM",
        modalities=["CT"],
        groups=[group],
    )

    response = client.get("/search/", {"query": "opacity", "language": "en"})

    assert response.status_code == 200
    assert _doc_ids(response) == [report.document_id]


def test_boolean_query_through_full_stack():
    client, group = _logged_in_client()
    both = _seed_report(
        "CT thorax shows pneumonia and a pleural effusion.",
        document_id="E2E-BOTH",
        modalities=["CT"],
        groups=[group],
    )
    one = _seed_report(
        "CT thorax shows pneumonia, no effusion.".replace("effusion", "clear"),
        document_id="E2E-ONE",
        modalities=["CT"],
        groups=[group],
    )

    # language=en so the english-stemmed 'effus' lexeme in the document matches
    # the english-stemmed query term.
    response = client.get(
        "/search/", {"query": "pneumonia AND effusion", "language": "en"}
    )

    ids = _doc_ids(response)
    assert both.document_id in ids
    assert one.document_id not in ids


def test_language_config_mismatch_disables_stemming():
    """Finding: when no language filter is selected the view resolves the query
    text-search config to 'simple' (``code_to_language("")``), while reports are
    indexed under their own language config (here english). Stemmed forms then
    silently fail to match.

    'opacities' is indexed under english (stem 'opac'); a 'simple'-config query
    for 'opacity' produces the un-stemmed lexeme 'opacity' and does NOT match,
    even though the exact word 'opacities' would. This documents a real
    searchability gap for users who do not pick a language.
    """
    client, group = _logged_in_client()
    report = _seed_report(
        "The lungs show multiple opacities bilaterally.",
        document_id="E2E-MISMATCH",
        modalities=["CT"],
        groups=[group],
    )

    # No language param -> query resolved with the 'simple' config, which does
    # not stem. The document, however, was indexed under english, so its stored
    # lexeme is the stem 'opac'. The mismatch means BOTH the stemmed query and
    # the exact surface form miss:
    assert _doc_ids(client.get("/search/", {"query": "opacity"})) == []
    assert _doc_ids(client.get("/search/", {"query": "opacities"})) == []

    # Selecting the matching language realigns the configs and the report is
    # found (control showing the report is genuinely indexed and retrievable).
    aligned = client.get("/search/", {"query": "opacity", "language": "en"})
    assert _doc_ids(aligned) == [report.document_id]


def test_modality_filter_propagates_into_query():
    """The ``modalities`` form filter must reach ``_build_filter_query`` and
    narrow results to reports carrying that modality."""
    client, group = _logged_in_client()
    ct = _seed_report(
        "pneumonia on CT", document_id="E2E-CT", modalities=["CT"], groups=[group]
    )
    mr = _seed_report(
        "pneumonia on MR", document_id="E2E-MR", modalities=["MR"], groups=[group]
    )

    response = client.get("/search/", {"query": "pneumonia", "modalities": ["CT"]})

    ids = _doc_ids(response)
    assert ct.document_id in ids
    assert mr.document_id not in ids


def test_patient_sex_filter_propagates_into_query():
    client, group = _logged_in_client()
    male = _seed_report(
        "pneumonia case",
        document_id="E2E-M",
        modalities=["CT"],
        patient_sex="M",
        groups=[group],
    )
    female = _seed_report(
        "pneumonia case",
        document_id="E2E-F",
        modalities=["CT"],
        patient_sex="F",
        groups=[group],
    )

    response = client.get("/search/", {"query": "pneumonia", "patient_sex": "F"})

    ids = _doc_ids(response)
    assert female.document_id in ids
    assert male.document_id not in ids


def test_study_description_filter_propagates_into_query():
    client, group = _logged_in_client()
    chest = _seed_report(
        "pneumonia case",
        document_id="E2E-CHEST",
        modalities=["CT"],
        study_description="CT Thorax with contrast",
        groups=[group],
    )
    brain = _seed_report(
        "pneumonia case",
        document_id="E2E-BRAIN",
        modalities=["CT"],
        study_description="MR Brain",
        groups=[group],
    )

    response = client.get(
        "/search/", {"query": "pneumonia", "study_description": "thorax"}
    )

    ids = _doc_ids(response)
    assert chest.document_id in ids  # icontains, case-insensitive
    assert brain.document_id not in ids


def test_study_date_filter_propagates_into_query():
    client, group = _logged_in_client()
    in_range = _seed_report(
        "pneumonia case",
        document_id="E2E-2023",
        modalities=["CT"],
        study_datetime=datetime(2023, 6, 15, 9, 0, tzinfo=UTC),
        groups=[group],
    )
    out_of_range = _seed_report(
        "pneumonia case",
        document_id="E2E-2010",
        modalities=["CT"],
        study_datetime=datetime(2010, 1, 1, 9, 0, tzinfo=UTC),
        groups=[group],
    )

    response = client.get(
        "/search/",
        {
            "query": "pneumonia",
            "study_date_from": "2023-01-01",
            "study_date_till": "2023-12-31",
        },
    )

    ids = _doc_ids(response)
    assert in_range.document_id in ids
    assert out_of_range.document_id not in ids


def test_age_filter_propagates_into_query():
    """``age_from``/``age_till`` map to ``patient_age`` bounds (a generated column
    from study_datetime - patient_birth_date)."""
    client, group = _logged_in_client()
    # patient_age is computed; pick birth/study dates that yield a ~40yo and ~80yo.
    young = _seed_report(
        "pneumonia case",
        document_id="E2E-YOUNG",
        modalities=["CT"],
        patient_birth_date=datetime(1983, 1, 1).date(),
        study_datetime=datetime(2023, 1, 1, tzinfo=UTC),  # ~40
        groups=[group],
    )
    old = _seed_report(
        "pneumonia case",
        document_id="E2E-OLD",
        modalities=["CT"],
        patient_birth_date=datetime(1943, 1, 1).date(),
        study_datetime=datetime(2023, 1, 1, tzinfo=UTC),  # ~80
        groups=[group],
    )

    # Age window 30..50 (multiples of 10, valid for the form) -> only the ~40yo.
    response = client.get(
        "/search/", {"query": "pneumonia", "age_from": 30, "age_till": 50}
    )

    ids = _doc_ids(response)
    assert young.document_id in ids
    assert old.document_id not in ids


def test_no_match_real_query_returns_empty():
    client, group = _logged_in_client()
    _seed_report(
        "CT thorax normal study",
        document_id="E2E-NONE",
        modalities=["CT"],
        groups=[group],
    )

    response = client.get("/search/", {"query": "rumpelstiltskin"})

    assert response.status_code == 200
    assert _doc_ids(response) == []
    assert response.context["total_count"] == 0


def test_hostile_query_through_real_stack_does_not_500():
    """A malformed/SQL-shaped query routed through the real parser + raw tsquery
    provider must render a normal 200, not surface a DB ProgrammingError."""
    client, group = _logged_in_client()
    _seed_report(
        "CT thorax pneumonia",
        document_id="E2E-HOSTILE",
        modalities=["CT"],
        groups=[group],
    )

    response = client.get(
        "/search/", {"query": "pneumonia; DROP TABLE reports;--"}
    )

    assert response.status_code == 200
    # The report still exists (no injection executed).
    from radis.reports.models import Report

    assert Report.objects.filter(document_id="E2E-HOSTILE").exists()


def test_search_does_not_leak_reports_from_other_groups():
    """A user must only see reports belonging to their active group.

    The view passes ``group=active_group.pk`` into ``SearchFilters`` and
    ``radis.pgsearch.providers._build_filter_query`` now filters on it (mirroring
    ``Report.objects.filter(groups=active_group)`` used by the report list/detail
    views), so a report associated solely with an unrelated group is not
    returned. This is the security-critical cross-group isolation guarantee.
    """
    client, my_group = _logged_in_client()
    other_group = GroupFactory.create()
    leaked = _seed_report(
        "confidential pneumonia finding",
        document_id="E2E-OTHERGROUP",
        modalities=["CT"],
    )
    # Belongs ONLY to a group the searching user is not a member of.
    leaked.groups.set([other_group])

    response = client.get("/search/", {"query": "pneumonia"})

    assert response.status_code == 200
    assert leaked.document_id not in _doc_ids(response)


def test_search_returns_reports_from_active_group():
    """Positive counterpart to the leak test: a report belonging to the user's
    active group IS returned (the group filter must not over-restrict and hide
    legitimate same-group results)."""
    client, my_group = _logged_in_client()
    mine = _seed_report(
        "pneumonia finding in my group",
        document_id="E2E-MYGROUP",
        modalities=["CT"],
        groups=[my_group],
    )
    other_group = GroupFactory.create()
    leaked = _seed_report(
        "pneumonia finding in another group",
        document_id="E2E-NOTMYGROUP",
        modalities=["CT"],
        groups=[other_group],
    )

    response = client.get("/search/", {"query": "pneumonia"})

    assert response.status_code == 200
    ids = _doc_ids(response)
    assert mine.document_id in ids
    assert leaked.document_id not in ids
