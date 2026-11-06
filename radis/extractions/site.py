from collections.abc import Callable, Iterable
from typing import NamedTuple

from radis.search.site import Search


class ExtractionRetrievalProvider(NamedTuple):
    """A class representing a retrieval provider.

    Attributes:
    - name: The name of the retrieval provider.
    - count: Counts the reports retrieve() yields for a search, at most
      ``Search.limit`` of them (None counts all). Callers pass one past the
      maximum they accept, so an over-large cohort is not counted in full.
    - retrieve: Yields the document IDs of every report matching a search, or
      the first ``Search.limit`` of them when a limit is set.
    """

    name: str
    count: Callable[[Search], int]
    retrieve: Callable[[Search], Iterable[str]]


extraction_retrieval_provider: ExtractionRetrievalProvider | None = None


def register_extraction_retrieval_provider(provider: ExtractionRetrievalProvider):
    global extraction_retrieval_provider
    extraction_retrieval_provider = provider
