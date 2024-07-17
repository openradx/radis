from typing import Any

from radis.reports.models import Report
from radis.search.site import ReportDocument


def _dictify_report_for_pgearch(report: Report) -> dict[str, Any]:
    return {
        "document_id": report.document_id,
        "language": report.language.code,
        "groups": [group.pk for group in report.groups.all()],
        "pacs_aet": report.pacs_aet,
        "pacs_name": report.pacs_name,
        "pacs_link": report.pacs_link,
        "patient_birth_date": report.patient_birth_date,
        "patient_age": report.patient_age,
        "patient_sex": report.patient_sex,
        "study_description": report.study_description,
        "study_datetime": report.study_datetime,
        "modalities": report.modality_codes,
        "body": report.body.strip(),
    }


def document_from_pgsearch_response(record: Report) -> ReportDocument:
    return ReportDocument(
        relevance=record.rank,
        document_id=record.document_id,
        pacs_name=record.pacs_name,
        pacs_link=record.pacs_link,
        patient_birth_date=record.patient_birth_date,
        patient_age=record.patient_age,
        patient_sex=record.patient_sex,
        study_description=record.study_description,
        study_datetime=record.study_datetime,
        modalities=record.modality_codes,
        summary=record.body,
    )
