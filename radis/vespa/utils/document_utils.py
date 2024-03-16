from datetime import date, datetime, time
from typing import Any

from radis.reports.models import Report
from radis.search.site import ReportDocument

from ..vespa_app import REPORT_SCHEMA_NAME, vespa_app


def _dictify_report_for_vespa(report: Report) -> dict[str, Any]:
    """Dictify the report for Vespa.

    Must be in the same format as schema in vespa_app.py
    """
    # Vespa can't store dates and datetimes natively, so we store them as a number.
    patient_birth_date = int(datetime.combine(report.patient_birth_date, time()).timestamp())
    study_datetime = int(report.study_datetime.timestamp())

    return {
        "document_id": report.document_id,
        "language": report.language,
        "groups": [group.id for group in report.groups.all()],
        "pacs_aet": report.pacs_aet,
        "pacs_name": report.pacs_name,
        "patient_birth_date": patient_birth_date,
        "patient_age": report.patient_age,
        "patient_sex": report.patient_sex,
        "study_description": report.study_description,
        "study_datetime": study_datetime,
        "modalities": report.modality_codes,
        "links": report.links,
        "body": report.body.strip(),
    }


def fetch_document(document_id: str) -> dict[str, Any]:
    response = vespa_app.get_client().get_data(REPORT_SCHEMA_NAME, document_id)

    if response.get_status_code() != 200:
        message = response.get_json()
        raise Exception(f"Error while fetching document from Vespa: {message}")

    return response.get_json()


def create_document(document_id: str, report: Report) -> None:
    fields = _dictify_report_for_vespa(report)
    response = vespa_app.get_client().feed_data_point(REPORT_SCHEMA_NAME, document_id, fields)

    if response.get_status_code() != 200:
        message = response.get_json()
        raise Exception(f"Error while feeding document to Vespa: {message}")


def update_document(document_id: str, report: Report) -> None:
    fields = _dictify_report_for_vespa(report)
    response = vespa_app.get_client().update_data(REPORT_SCHEMA_NAME, document_id, fields)

    if response.get_status_code() != 200:
        message = response.get_json()
        raise Exception(f"Error while updating document on Vespa: {message}")


def delete_document(document_id: str) -> None:
    response = vespa_app.get_client().delete_data(REPORT_SCHEMA_NAME, document_id)

    if response.get_status_code() != 200:
        message = response.get_json()
        raise Exception(f"Error while deleting document on Vespa: {message}")


def document_from_vespa_response(record: dict[str, Any]) -> ReportDocument:
    patient_birth_date = date.fromtimestamp(record["fields"]["patient_birth_date"])
    study_datetime = datetime.fromtimestamp(record["fields"]["study_datetime"])

    return ReportDocument(
        relevance=record["relevance"],
        document_id=record["fields"]["document_id"],
        pacs_name=record["fields"]["pacs_name"],
        patient_birth_date=patient_birth_date,
        patient_age=record["fields"]["patient_age"],
        patient_sex=record["fields"]["patient_sex"],
        study_description=record["fields"].get("study_description", ""),
        study_datetime=study_datetime,
        modalities=record["fields"].get("modalities", []),
        links=record["fields"].get("links", []),
        body=record["fields"]["body"],
    )
