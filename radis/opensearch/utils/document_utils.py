from typing import Any

from radis.opensearch.client import get_client
from radis.reports.models import Report
from radis.search.site import ReportDocument


def _dictify_report_for_opensearch(report: Report) -> dict[str, Any]:
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


def create_documents(report_ids: list[int]) -> None:
    client = get_client()
    reports = Report.objects.filter(id__in=report_ids)

    for report in reports:
        index_name = f"reports_{report.language.code}"
        body = _dictify_report_for_opensearch(report)
        client.create(index=index_name, id=report.document_id, body=body)


def update_documents(report_ids: list[int]) -> None:
    client = get_client()
    reports = Report.objects.filter(id__in=report_ids)

    for report in reports:
        index_name = f"reports_{report.language.code}"
        body = _dictify_report_for_opensearch(report)
        client.update(index=index_name, id=report.document_id, body={"doc": body})


def delete_documents(document_ids: list[str]) -> None:
    client = get_client()

    for document_id in document_ids:
        client.delete(index="reports", id=document_id)


def fetch_document(document_id: str) -> dict[str, Any]:
    client = get_client()
    return client.get(index="reports", id=document_id)


def document_from_opensearch_response(record: dict[str, Any]) -> ReportDocument:
    source = record["_source"]
    return ReportDocument(
        relevance=record["_score"],
        document_id=record["_id"],
        pacs_name=source["pacs_name"],
        pacs_link=source["pacs_link"],
        patient_birth_date=source["patient_birth_date"],
        patient_age=source["patient_age"],
        patient_sex=source["patient_sex"],
        study_description=source["study_description"],
        study_datetime=source["study_datetime"],
        modalities=source["modalities"],
        summary=(" <strong>...</strong> ".join(record["highlight"]["body"])).strip(),
    )
