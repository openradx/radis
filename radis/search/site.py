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
    """
    A class representing a search.

    If both offset and limit are set to 0, then the search provider
    should return the most accurate total count it can calculate.

    Attributes:
    - query (str): The query to search.
    - offset (int): The offset of the search results.
    - limit (int): The limit of the search results.
    - filters (SearchFilters): The filters to apply to the search.
    """

    query: str
    offset: int = 0
    limit: int = 10
    filters: SearchFilters = SearchFilters()


SearchHandler = Callable[[Search], SearchResult]


class SearchProvider(NamedTuple):
    """
    A class representing a search provider.

    Attributes:
    - name (str): The name of the search provider.
    - handler (SearchHandler): The function that handles the search.
    - max_results (int): The maximum number of results that can be returned.
      Must be smaller than offset + limit when searching.
    - info_template (str): The template to be rendered as info.
    """

    name: str
    handler: SearchHandler
    max_results: int
    info_template: str


search_providers: dict[str, SearchProvider] = {}


def register_search_provider(search_provider: SearchProvider) -> None:
    """Register a search provider."""
    search_providers[search_provider.name] = search_provider
