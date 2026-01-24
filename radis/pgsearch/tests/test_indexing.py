import pytest

from radis.pgsearch.models import ReportSearchVector
from radis.pgsearch.utils.indexing import bulk_upsert_report_search_vectors
from radis.reports.models import Language, Report


@pytest.mark.django_db
def test_bulk_index_matches_signal_vector() -> None:
    language = Language.objects.create(code="en")
    report = Report.objects.create(
        document_id="DOC-INDEX",
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
        body="Findings: No acute abnormality.",
        language=language,
    )

    signal_vector = ReportSearchVector.objects.get(report=report).search_vector
    ReportSearchVector.objects.filter(report=report).delete()

    bulk_upsert_report_search_vectors([report.pk])
    bulk_vector = ReportSearchVector.objects.get(report=report).search_vector

    assert signal_vector == bulk_vector
