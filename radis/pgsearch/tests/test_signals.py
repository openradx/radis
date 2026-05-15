from unittest.mock import patch

import pytest

from radis.reports.factories import ReportFactory


@pytest.mark.django_db
def test_report_save_enqueues_embed_reports():
    from radis.reports.models import Language, Report

    language = Language.objects.create(code="en")
    with patch("radis.pgsearch.signals.enqueue_embed_reports") as enqueue:
        report = Report.objects.create(
            document_id="DOC-SIGNAL-1",
            pacs_aet="PACS",
            pacs_name="PACS",
            pacs_link="",
            patient_id="P1",
            patient_birth_date="1980-01-01",
            patient_sex="M",
            study_description="Study",
            study_datetime="2024-01-01T00:00:00Z",
            study_instance_uid="1.2.3.4",
            accession_number="ACC1",
            body="Body.",
            language=language,
        )
    enqueue.assert_called_once_with([report.pk])


@pytest.mark.django_db
def test_report_update_also_enqueues_embed_reports():
    report = ReportFactory.create()
    with patch("radis.pgsearch.signals.enqueue_embed_reports") as enqueue:
        report.body = "Updated body"
        report.save()
    enqueue.assert_called_once_with([report.pk])
