from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Callable, Literal, NamedTuple

from radis.reports.models import Report


class ReportDocument(NamedTuple):
    relevance: float | None
    document_id: str
    pacs_name: str
    patient_birth_date: date
    patient_age: int
    patient_sex: Literal["F", "M", "U"]
    study_description: str
    study_datetime: datetime
    modalities: list[str]
    links: list[str]
    body: str

    @property
    def full_report(self) -> Report:
        return Report.objects.get(document_id=self.document_id)


class SearchResult(NamedTuple):
    total_count: int
    coverage: float | None
    documents: list[ReportDocument]


@dataclass
class SearchFilters:
    study_date_from: date | None = None
    study_date_till: date | None = None
    study_description: str = ""
    modalities: list[str] = field(default_factory=list)
    patient_sex: Literal["M", "F"] | None = None
    patient_age_from: int | None = None
    patient_age_till: int | None = None


class Search(NamedTuple):
    query: str
    offset: int = 0
    limit: int = 10
    filters: SearchFilters = SearchFilters()


SearchHandler = Callable[[Search], SearchResult]


class SearchProvider(NamedTuple):
    name: str
    handler: SearchHandler
    info_template: str


search_providers: dict[str, SearchProvider] = {}


def register_search_provider(
    name: str,
    handler: SearchHandler,
    info_template: str,
) -> None:
    """Register a search handler.

    The name can be selected by the user in the search form. The searcher is called
    when the user submits the form and returns the results. The template name is
    the partial to be rendered as info below the search form.
    """
    search_providers[name] = SearchProvider(name, handler, info_template)
