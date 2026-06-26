import pytest

from radis.labels.factories import LabelFactory, LabelResultFactory
from radis.labels.models import LabelResult
from radis.pgsearch.models import ReportSearchVector
from radis.pgsearch.providers import _build_filter_query
from radis.reports.factories import ReportFactory
from radis.reports.models import Language
from radis.search.site import SearchFilters


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
def test_label_filter_matches_any_label() -> None:
    """When multiple labels are requested, a report surfacing ANY of them matches (OR)."""
    language = Language.objects.get_or_create(code="en")[0]

    # report_edema surfaces only "edema"
    report_edema = ReportFactory.create(language=language)
    label_edema = LabelFactory.create(name="edema")
    label_pneumonia = LabelFactory.create(name="pneumonia")
    LabelResultFactory.create(
        report=report_edema, label=label_edema, value=LabelResult.Value.PRESENT
    )

    # report_pneumonia surfaces only "pneumonia"
    report_pneumonia = ReportFactory.create(language=language)
    LabelResultFactory.create(
        report=report_pneumonia, label=label_pneumonia, value=LabelResult.Value.PRESENT
    )

    # report_both surfaces both labels
    report_both = ReportFactory.create(language=language)
    LabelResultFactory.create(
        report=report_both, label=label_edema, value=LabelResult.Value.PRESENT
    )
    LabelResultFactory.create(
        report=report_both, label=label_pneumonia, value=LabelResult.Value.PRESENT
    )

    # report_neither surfaces nothing relevant
    report_neither = ReportFactory.create(language=language)

    fq = _build_filter_query(SearchFilters(group=0, labels=["edema", "pneumonia"]))
    matched_ids = set(ReportSearchVector.objects.filter(fq).values_list("report_id", flat=True))

    assert report_edema.pk in matched_ids
    assert report_pneumonia.pk in matched_ids
    assert report_both.pk in matched_ids
    assert report_neither.pk not in matched_ids


