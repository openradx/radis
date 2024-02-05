import logging
from dataclasses import dataclass
from datetime import date, datetime, time
from typing import Any, Literal

from rest_framework.status import HTTP_200_OK
from vespa.io import VespaQueryResponse

from radis.core.models import AppSettings
from radis.reports.models import Report
from radis.reports.site import ReportEventType

from .utils.search_utils import extract_document_id
from .vespa_app import REPORT_SCHEMA_NAME, vespa_app

logger = logging.getLogger(__name__)


class SearchAppSettings(AppSettings):
    class Meta:
        verbose_name_plural = "Search app settings"


class ReportDocument:
    def __init__(self, report: Report) -> None:
        self.report = report

    def _dictify_for_vespa(self) -> dict[str, Any]:
        """Dictify the report for Vespa.

        Must be in the same format as schema in vespa_app.py
        """
        # Vespa can't store dates and datetimes natively, so we store them as a number.
        patient_birth_date = int(
            datetime.combine(self.report.patient_birth_date, time()).timestamp()
        )
        study_datetime = int(self.report.study_datetime.timestamp())

        return {
            "groups": [group.id for group in self.report.groups.all()],
            "pacs_aet": self.report.pacs_aet,
            "pacs_name": self.report.pacs_name,
            "patient_birth_date": patient_birth_date,
            "patient_sex": self.report.patient_sex,
            "study_description": self.report.study_description,
            "study_datetime": study_datetime,
            "modalities_in_study": self.report.modalities_in_study,
            "references": self.report.references,
            "body": self.report.body.strip(),
        }

    def fetch(self) -> dict[str, Any]:
        response = vespa_app.get_client().get_data(REPORT_SCHEMA_NAME, self.report.document_id)

        if response.get_status_code() != HTTP_200_OK:
            message = response.get_json()
            raise Exception(f"Error while fetching report from Vespa: {message}")

        return response.get_json()

    def create(self) -> None:
        fields = self._dictify_for_vespa()
        response = vespa_app.get_client().feed_data_point(
            REPORT_SCHEMA_NAME, self.report.document_id, fields
        )

        if response.get_status_code() != HTTP_200_OK:
            message = response.get_json()
            raise Exception(f"Error while feeding report to Vespa: {message}")

    def update(self) -> None:
        fields = self._dictify_for_vespa()
        response = vespa_app.get_client().update_data(
            REPORT_SCHEMA_NAME, self.report.document_id, fields
        )

        if response.get_status_code() != HTTP_200_OK:
            message = response.get_json()
            raise Exception(f"Error while updating report on Vespa: {message}")

    def delete(self) -> None:
        response = vespa_app.get_client().delete_data(REPORT_SCHEMA_NAME, self.report.document_id)

        if response.get_status_code() != HTTP_200_OK:
            message = response.get_json()
            raise Exception(f"Error while deleting report on Vespa: {message}")


@dataclass(kw_only=True)
class ReportSummary:
    relevance: float | None
    document_id: str
    pacs_name: str
    patient_birth_date: date
    patient_sex: Literal["F", "M", "U"]
    study_description: str
    study_datetime: datetime
    modalities_in_study: list[str]
    references: list[str]
    body: str

    @staticmethod
    def from_vespa_response(record: dict) -> "ReportSummary":
        patient_birth_date = date.fromtimestamp(record["fields"]["patient_birth_date"])
        study_datetime = datetime.fromtimestamp(record["fields"]["study_datetime"])

        return ReportSummary(
            relevance=record["relevance"],
            document_id=extract_document_id(record["id"]),
            pacs_name=record["fields"]["pacs_name"],
            patient_birth_date=patient_birth_date,
            patient_sex=record["fields"]["patient_sex"],
            study_description=record["fields"].get("study_description", ""),
            study_datetime=study_datetime,
            modalities_in_study=record["fields"].get("modalities_in_study", []),
            references=record["fields"].get("references", []),
            body=record["fields"]["body"],
        )

    @property
    def report_full(self) -> Report:
        return Report.objects.get(document_id=self.document_id)


@dataclass
class ReportQuery:
    total_count: int
    coverage: float
    documents: int
    reports: list[ReportSummary]

    @staticmethod
    def from_vespa_response(response: VespaQueryResponse):
        json = response.json
        return ReportQuery(
            total_count=json["root"]["fields"]["totalCount"],
            coverage=json["root"]["coverage"]["coverage"],
            documents=json["root"]["coverage"]["documents"],
            reports=[ReportSummary.from_vespa_response(hit) for hit in response.hits],
        )

    @staticmethod
    def query_reports(query: str, offset: int = 0, page_size: int = 100) -> "ReportQuery":
        client = vespa_app.get_client()
        response = client.query(
            {
                "yql": "select * from report where userQuery()",
                "query": query,
                "type": "web",
                "hits": page_size,
                "offset": offset,
            }
        )
        return ReportQuery.from_vespa_response(response)


def handle_report(event_type: ReportEventType, report: Report):
    # Sync reports with Vespa
    if event_type == "created":
        ReportDocument(report).create()
    elif event_type == "updated":
        ReportDocument(report).update()
    elif event_type == "deleted":
        ReportDocument(report).delete()


def fetch_document(report: Report) -> dict[str, Any]:
    doc = ReportDocument(report).fetch()
    return doc
