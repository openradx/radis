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
    patient_age: int
    patient_sex: Literal["F", "M", "U"]
    study_description: str
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
        - group: Filter reports that belong to the given group (normally the active group
          of the user)
        - language: Filter reports that have the given language
        - modalities: Filter reports that have at least one of the given modalities
        - study_date_from: Filter only reports from this date
        - study_date_till: Filter only reports until this date
        - study_description: Filter only reports that have the given description (partial match)
        - patient_sex: Filter only reports that have the given sex
        - patient_age_from: Filter only reports where the patient is at least this age
        - patient_age_till: Filter only reports where the patient is at most this age
    """

    group: int  # TODO: Rename to group_id
    language: str = ""  # TODO: Rename to language_code
    modalities: list[str] = field(default_factory=list)
    study_date_from: date | None = None
    study_date_till: date | None = None
    study_description: str = ""
    patient_sex: Literal["M", "F"] | None = None
    patient_age_from: int | None = None
    patient_age_till: int | None = None
    patient_id: str | None = None
    created_after: datetime | None = None
    created_before: datetime | None = None


class Search(NamedTuple):
    """A class representing a search.

    If both offset and limit are set to 0, then the search provider
    should return the most accurate total count it can calculate.

    Attributes:
    - query: The query to search.
    - filters: The filters to apply to the search.
    - offset: The offset of the search results.
    - limit: The size limit of the search results.
    """

    query: QueryNode
    filters: SearchFilters
    offset: int = 0
    limit: int | None = 10


class SearchProvider(NamedTuple):
    """A class representing a search provider.

    Attributes:
    - name: The name of the search provider.
    - search: The function that handles the search.
    - max_results: The maximum number of results that can be fetched by a search.
        Must be smaller than offset + limit when searching.
    """

    name: str
    search: Callable[[Search], SearchResult]
    max_results: int


search_provider: SearchProvider | None = None


def register_search_provider(provider: SearchProvider) -> None:
    """Register a search provider."""
    global search_provider
    search_provider = provider
