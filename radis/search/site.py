from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Callable, Literal, NamedTuple

from radis.reports.models import Report
from radis.search.utils.query_parser import QueryNode


class ReportDocument(NamedTuple):
    relevance: float | None
    document_id: str
    pacs_name: str
    pacs_link: str
    patient_birth_date: date
    patient_age: int
    patient_sex: Literal["F", "M", "U"]
    study_description: str
    study_datetime: datetime
    modalities: list[str]
    summary: str

    @property
    def full_report(self) -> Report:
        return Report.objects.get(document_id=self.document_id)


class SearchResult(NamedTuple):
    total_count: int
    total_relation: Literal["exact", "at_least", "approximately"]
    documents: list[ReportDocument]


@dataclass
class SearchFilters:
    """Search filters

    Attributes:
        - group (int): Filter reports that belong to the given group (normally the active group
          of the user)
        - language (str): Filter reports that have the given language
        - modalities (list[str]): Filter reports that have at least one of the given modalities
        - study_date_from (date | None): Filter only reports from this date
        - study_date_till (date | None): Filter only reports until this date
        - study_description (str): Filter only reports that have the given description
          (partial match)
        - patient_sex (Literal["M", "F"] | None): Filter only reports that have the given sex
        - patient_age_from (int | None): Filter only reports where the patient is at least this age
        - patient_age_till (int | None): Filter only reports where the patient is at most this age
    """

    group: int
    language: str
    modalities: list[str] = field(default_factory=list)
    study_date_from: date | None = None
    study_date_till: date | None = None
    study_description: str = ""
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
    - filters (SearchFilters): The filters to apply to the search.
    - offset (int): The offset of the search results.
    - limit (int | None): The size limit of the search results.
    """

    query: QueryNode
    filters: SearchFilters
    offset: int = 0
    limit: int | None = 10


class SearchProvider(NamedTuple):
    """
    A class representing a search provider.

    Attributes:
    - name (str): The name of the search provider.
    - search (Callable[[Search], SearchResult]): The function that handles the search.
    - max_results (int): The maximum number of results that can be fetched by a search.
      Must be smaller than offset + limit when searching.
    """

    name: str
    search: Callable[[Search], SearchResult]
    max_results: int


search_providers: dict[str, SearchProvider] = {}


def register_search_provider(search_provider: SearchProvider) -> None:
    """Register a search provider."""
    search_providers[search_provider.name] = search_provider
