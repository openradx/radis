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
