import logging
from datetime import date, datetime
from typing import Literal, NamedTuple

from radis.core.models import AppSettings
from radis.reports.models import Report

logger = logging.getLogger(__name__)


class SearchAppSettings(AppSettings):
    class Meta:
        verbose_name_plural = "Search app settings"


class ReportDocument(NamedTuple):
    relevance: float | None
    document_id: str
    pacs_name: str
    patient_birth_date: date
    patient_sex: Literal["F", "M", "U"]
    study_description: str
    study_datetime: datetime
    modalities_in_study: list[str]
    links: list[str]
    body: str

    @property
    def full_report(self) -> Report:
        return Report.objects.get(document_id=self.document_id)


class SearchResult(NamedTuple):
    total_count: int | None
    coverage: float | None
    documents: list[ReportDocument]
