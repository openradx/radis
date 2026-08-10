import pytest
from adit_radis_shared.accounts.factories import GroupFactory

from radis.labels.factories import LabelFactory, LabelResultFactory
from radis.labels.models import LabelResult
from radis.pgsearch.models import ReportSearchIndex
from radis.pgsearch.providers import _build_filter_query
from radis.reports.factories import ReportFactory
from radis.reports.models import Language
from radis.search.site import SearchFilters


def _make_report(group, language):
    """Create a report visible to ``group`` (the filter query is group-scoped)."""
    report = ReportFactory.create(language=language)
    report.groups.add(group)
    return report


@pytest.mark.django_db
def test_label_filter_includes_surfacing_result() -> None:
    """A report with a PRESENT LabelResult for 'edema' must be returned by the filter."""
    language = Language.objects.get_or_create(code="en")[0]
    group = GroupFactory.create()
    report = _make_report(group, language)
    label = LabelFactory.create(name="edema")
    LabelResultFactory.create(report=report, label=label, value=LabelResult.Value.PRESENT)

    # ReportSearchIndex is created automatically by signal on Report save.
    assert ReportSearchIndex.objects.filter(report=report).exists()

    fq = _build_filter_query(SearchFilters(group=group.pk, labels=["edema"]))
    matched_ids = set(ReportSearchIndex.objects.filter(fq).values_list("report_id", flat=True))

    assert report.pk in matched_ids


@pytest.mark.django_db
def test_label_filter_excludes_absent_result() -> None:
    """A report whose only LabelResult for 'edema' is ABSENT must NOT be returned."""
    language = Language.objects.get_or_create(code="en")[0]
    group = GroupFactory.create()
    report = _make_report(group, language)
    label = LabelFactory.create(name="edema")
    LabelResultFactory.create(report=report, label=label, value=LabelResult.Value.ABSENT)

    assert ReportSearchIndex.objects.filter(report=report).exists()

    fq = _build_filter_query(SearchFilters(group=group.pk, labels=["edema"]))
    matched_ids = set(ReportSearchIndex.objects.filter(fq).values_list("report_id", flat=True))

    assert report.pk not in matched_ids


@pytest.mark.django_db
def test_label_filter_matches_any_label() -> None:
    """When multiple labels are requested, a report surfacing ANY of them matches (OR)."""
    language = Language.objects.get_or_create(code="en")[0]
    group = GroupFactory.create()

    # report_edema surfaces only "edema"
    report_edema = _make_report(group, language)
    label_edema = LabelFactory.create(name="edema")
    label_pneumonia = LabelFactory.create(name="pneumonia")
    LabelResultFactory.create(
        report=report_edema, label=label_edema, value=LabelResult.Value.PRESENT
    )

    # report_pneumonia surfaces only "pneumonia"
    report_pneumonia = _make_report(group, language)
    LabelResultFactory.create(
        report=report_pneumonia, label=label_pneumonia, value=LabelResult.Value.PRESENT
    )

    # report_both surfaces both labels
    report_both = _make_report(group, language)
    LabelResultFactory.create(
        report=report_both, label=label_edema, value=LabelResult.Value.PRESENT
    )
    LabelResultFactory.create(
        report=report_both, label=label_pneumonia, value=LabelResult.Value.PRESENT
    )

    # report_neither surfaces nothing relevant
    report_neither = _make_report(group, language)

    fq = _build_filter_query(SearchFilters(group=group.pk, labels=["edema", "pneumonia"]))
    matched_ids = set(ReportSearchIndex.objects.filter(fq).values_list("report_id", flat=True))

    assert report_edema.pk in matched_ids
    assert report_pneumonia.pk in matched_ids
    assert report_both.pk in matched_ids
    assert report_neither.pk not in matched_ids
