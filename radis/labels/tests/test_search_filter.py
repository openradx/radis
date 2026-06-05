from unittest.mock import patch

import pytest
from adit_radis_shared.accounts.factories import GroupFactory, UserFactory
from django.test import Client

from radis.labels.factories import LabelFactory, LabelResultFactory
from radis.labels.models import LabelResult
from radis.pgsearch.models import ReportSearchVector
from radis.pgsearch.providers import _build_filter_query
from radis.reports.factories import ReportFactory
from radis.reports.models import Language
from radis.search.site import Search, SearchFilters, SearchProvider, SearchResult
from radis.search.utils.query_parser import QueryParser


@pytest.mark.django_db
def test_label_filter_includes_surfacing_result() -> None:
    """A report with a PRESENT LabelResult for 'edema' must be returned by the filter."""
    language = Language.objects.get_or_create(code="en")[0]
    report = ReportFactory.create(language=language)
    label = LabelFactory.create(name="edema")
    LabelResultFactory.create(report=report, label=label, value=LabelResult.Value.PRESENT)

    # ReportSearchVector is created automatically by signal on Report save.
    assert ReportSearchVector.objects.filter(report=report).exists()

    fq = _build_filter_query(SearchFilters(group=0, labels=["edema"]))
    matched_ids = set(ReportSearchVector.objects.filter(fq).values_list("report_id", flat=True))

    assert report.pk in matched_ids


@pytest.mark.django_db
def test_label_filter_excludes_absent_result() -> None:
    """A report whose only LabelResult for 'edema' is ABSENT must NOT be returned."""
    language = Language.objects.get_or_create(code="en")[0]
    report = ReportFactory.create(language=language)
    label = LabelFactory.create(name="edema")
    LabelResultFactory.create(report=report, label=label, value=LabelResult.Value.ABSENT)

    assert ReportSearchVector.objects.filter(report=report).exists()

    fq = _build_filter_query(SearchFilters(group=0, labels=["edema"]))
    matched_ids = set(ReportSearchVector.objects.filter(fq).values_list("report_id", flat=True))

    assert report.pk not in matched_ids


@pytest.mark.django_db
def test_label_filter_requires_all_labels() -> None:
    """When multiple labels are requested, only reports surfacing ALL of them match."""
    language = Language.objects.get_or_create(code="en")[0]

    # report_both has PRESENT for both "edema" and "pneumonia"
    report_both = ReportFactory.create(language=language)
    label_edema = LabelFactory.create(name="edema")
    label_pneumonia = LabelFactory.create(name="pneumonia")
    LabelResultFactory.create(
        report=report_both, label=label_edema, value=LabelResult.Value.PRESENT
    )
    LabelResultFactory.create(
        report=report_both, label=label_pneumonia, value=LabelResult.Value.PRESENT
    )

    # report_one has PRESENT only for "edema"
    report_one = ReportFactory.create(language=language)
    LabelResultFactory.create(report=report_one, label=label_edema, value=LabelResult.Value.PRESENT)

    fq = _build_filter_query(SearchFilters(group=0, labels=["edema", "pneumonia"]))
    matched_ids = set(ReportSearchVector.objects.filter(fq).values_list("report_id", flat=True))

    assert report_both.pk in matched_ids
    assert report_one.pk not in matched_ids


@pytest.mark.django_db
def test_search_view_extracts_label_filter_from_query(client: Client) -> None:
    """The search view strips `label:` tokens from the query and threads them into SearchFilters."""
    user = UserFactory.create(is_active=True)
    group = GroupFactory.create()
    user.groups.add(group)
    user.active_group = group
    user.save()
    client.force_login(user)

    captured: dict[str, Search] = {}

    def capturing_search(search: Search) -> SearchResult:
        captured["search"] = search
        return SearchResult(total_count=0, total_relation="exact", documents=[])

    provider = SearchProvider(name="Capturing", search=capturing_search, max_results=1000)

    with patch("radis.search.views.search_provider", provider):
        response = client.get("/search/", {"query": "chest label:edema"})

    assert response.status_code == 200
    search = captured["search"]
    assert search.filters.labels == ["edema"]
    # The free-text query no longer contains the label token.
    unparsed = QueryParser.unparse(search.query)
    assert "chest" in unparsed
    assert "label:" not in unparsed
