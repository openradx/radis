"""Tests for ``iter_extraction_result_rows`` (radis/extractions/utils/csv_export.py)."""

import pytest
from django.utils import timezone

from radis.extractions.factories import ExtractionInstanceFactory, ExtractionJobFactory
from radis.extractions.models import ExtractionInstance
from radis.extractions.utils.csv_export import iter_extraction_result_rows
from radis.reports.factories import LanguageFactory, ReportFactory
from radis.reports.models import Report


@pytest.mark.django_db
def test_iter_extraction_result_rows_skips_withdrawn_reports():
    job = ExtractionJobFactory.create()
    language = LanguageFactory.create(code="en")
    live_report = ReportFactory.create(language=language)
    withdrawn_report = ReportFactory.create(language=language)
    live_instance = ExtractionInstanceFactory.create(
        task__job=job, report=live_report, is_processed=True
    )
    ExtractionInstanceFactory.create(task__job=job, report=withdrawn_report, is_processed=True)
    Report.objects.filter(pk=withdrawn_report.pk).update(withdrawn_at=timezone.now())

    rows = list(iter_extraction_result_rows(job))

    assert rows[0] == ["instance_id", "report_id", "is_processed"]
    assert rows[1:] == [[str(live_instance.pk), str(live_report.pk), "yes"]]
    # Both instance rows survive; only the export skips the withdrawn one.
    assert ExtractionInstance.objects.filter(task__job=job).count() == 2
