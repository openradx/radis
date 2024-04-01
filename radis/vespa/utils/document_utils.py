import logging
from datetime import date, datetime, time
from typing import Any, Iterable

from django.db.models import QuerySet
from vespa.io import VespaResponse

from radis.reports.models import Report
from radis.search.site import ReportDocument

from ..vespa_app import REPORT_SCHEMA_NAME, vespa_app

logger = logging.getLogger(__name__)


def _dictify_report_for_vespa(report: Report) -> dict[str, Any]:
    """Dictify the report for Vespa.

    Must be in the same format as schema in vespa_app.py
    """
    # Vespa can't store dates and datetimes natively, so we store them as a number.
    patient_birth_date = int(datetime.combine(report.patient_birth_date, time()).timestamp())
    study_datetime = int(report.study_datetime.timestamp())

    return {
        "language": report.language.code,
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


def _generate_feedable_documents(reports: QuerySet[Report]) -> Iterable[dict]:
    for report in reports:
        fields = _dictify_report_for_vespa(report)
        yield {"id": report.document_id, "fields": fields}


def fetch_document(document_id: str) -> dict[str, Any]:
    response = vespa_app.get_client().get_data(REPORT_SCHEMA_NAME, document_id)

    if response.get_status_code() != 200:
        message = response.get_json()
        raise Exception(f"Error while fetching document from Vespa: {message}")

    return response.get_json()


def create_documents(report_ids: list[int]) -> None:
    reports = Report.objects.filter(id__in=report_ids)

    def callback(response: VespaResponse, id: str):
        if response.get_status_code() == 200:
            logger.debug(f"Successfully fed document with id {id} to Vespa")
        else:
            message = response.get_json()
            logger.error(f"Error while feeding document with id {id} to Vespa: {message}")

    vespa_app.get_client().feed_iterable(
        _generate_feedable_documents(reports), REPORT_SCHEMA_NAME, callback=callback
    )


def update_documents(report_ids: list[int]) -> None:
    reports = Report.objects.filter(id__in=report_ids)

    def callback(response: VespaResponse, id: str):
        if response.get_status_code() == 200:
            logger.debug(f"Successfully updated document with id {id} in Vespa")
            pass
        else:
            message = response.get_json()
            logger.error(f"Error while updating document with id {id} in Vespa: {message}")

    vespa_app.get_client().feed_iterable(
        _generate_feedable_documents(reports),
        REPORT_SCHEMA_NAME,
        operation_type="update",
        callback=callback,
    )


def delete_documents(document_ids: list[str]) -> None:
    def callback(response: VespaResponse, id: str):
        if response.get_status_code() == 200:
            logger.debug(f"Successfully deleted document with id {id} in Vespa")
        else:
            message = response.get_json()
            logger.error(f"Error while deleting document with id {id} in Vespa: {message}")

    vespa_app.get_client().feed_iterable(
        [{"id": id} for id in document_ids],
        REPORT_SCHEMA_NAME,
        operation_type="delete",
        callback=callback,
    )


def extract_document_id(documentid: str) -> str:
    # https://docs.vespa.ai/en/documents.html#document-ids
    return documentid.split(":")[-1]


def document_from_vespa_response(record: dict[str, Any]) -> ReportDocument:
    document_id = extract_document_id(record["fields"]["documentid"])
    patient_birth_date = date.fromtimestamp(record["fields"]["patient_birth_date"])
    study_datetime = datetime.fromtimestamp(record["fields"]["study_datetime"])

    return ReportDocument(
        relevance=record["relevance"],
        document_id=document_id,
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
