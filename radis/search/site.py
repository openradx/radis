from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Literal, NamedTuple

from radis.reports.models import Report
from radis.search.utils.query_parser import QueryNode


class ReportDocument(NamedTuple):
    relevance: float | None  # ts_rank of a full-text hit; None in semantic mode
    document_id: str
    pacs_name: str
    pacs_link: str
    patient_age: int
    patient_sex: Literal["F", "M", "U"]
    study_description: str
    modalities: list[str]
    # HTML-safe markup: escaped report text plus <em> highlights, marked safe by the
    # provider. A plain string from another provider is autoescaped by the template.
    summary: str
    similarity: float | None = None  # 1 - cosine distance when measured, else None
    score: float = 0.0  # the value that decided the position in the result list

    @property
    def full_report(self) -> Report:
        return Report.objects.get(document_id=self.document_id)


class SearchResult(NamedTuple):
    total_count: int
    total_relation: Literal["exact", "at_least", "approximately"]
    documents: list[ReportDocument]
    # True when the semantic part of the search (the re-rank, or semantic mode itself)
    # could not run because no embedding was available.
    degraded: bool = False


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
        - labels: only reports having a surfacing-bucket (PRESENT/LIKELY/POSSIBLE) result for
          ANY of the given label names
        - group: Group whose reports may be searched (access control). ``None`` is
          fail-closed: it only matches reports assigned to no group at all.
        - updated_after: Filter only reports whose ``updated_at`` is at or after
          this timestamp (used for incremental refreshes).
    """

    group: int | None = None  # TODO: Rename to group_id
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
    labels: list[str] = field(default_factory=list)
    updated_after: datetime | None = None


@dataclass(frozen=True, kw_only=True)
class Search:
    """Inputs every search mode shares.

    Offset and limit both 0 ask the provider for the most accurate total count it
    can give; extractions rely on this.
    """

    filters: SearchFilters
    offset: int = 0
    limit: int | None = 10


@dataclass(frozen=True)
class SimilarityRerank:
    """Re-order the top SEARCH_RERANK_WINDOW keyword hits by a blend of their
    similarity to ``text`` and their keyword rank."""

    text: str

    def __post_init__(self) -> None:
        if not self.text.strip():
            raise ValueError("The re-rank text must not be blank.")


@dataclass(frozen=True, kw_only=True)
class FullTextSearch(Search):
    """Exactly the keyword hits, optionally re-ranked within the window."""

    query: QueryNode
    rerank: SimilarityRerank | None = None


@dataclass(frozen=True, kw_only=True)
class SemanticSearch(Search):
    """The reports most similar to ``text``, in similarity order."""

    text: str
    # Overrides SEARCH_SEMANTIC_MIN_SIMILARITY for this search; None means the setting.
    min_similarity: float | None = None

    def __post_init__(self) -> None:
        if not self.text.strip():
            raise ValueError("The semantic search text must not be blank.")
        if self.min_similarity is not None and not 0.0 <= self.min_similarity <= 1.0:
            raise ValueError("min_similarity must be between 0 and 1.")


SearchRequest = FullTextSearch | SemanticSearch


class SearchProvider(NamedTuple):
    """A registered search backend.

    Attributes:
    - name: The name of the search provider.
    - search: Runs one request of either mode.
    - max_results: The paging cap of a full-text list; offset + limit must stay below it.
    - semantic: Whether SemanticSearch requests and re-ranks are available (an
      embedding model is configured).
    """

    name: str
    search: Callable[[SearchRequest], SearchResult]
    max_results: int
    semantic: bool = False


search_provider: SearchProvider | None = None


def register_search_provider(provider: SearchProvider) -> None:
    """Register a search provider."""
    global search_provider
    search_provider = provider
