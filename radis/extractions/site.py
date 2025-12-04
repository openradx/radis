from typing import Callable, Iterable, NamedTuple

from radis.search.site import Search


class ExtractionRetrievalProvider(NamedTuple):
    """A class representing a retrieval provider.

    Attributes:
    - name: The name of the retrieval provider.
    - count: A function that counts the number of results for a search.
    - retrieve: A function that retrieves the document IDs
      for a search.
    - max_results: The maximum number of results that can be retrieved by this
      provider, or None if there is no limit.
    """

    name: str
    count: Callable[[Search], int]
    retrieve: Callable[[Search], Iterable[str]]
    max_results: int | None


extraction_retrieval_provider: ExtractionRetrievalProvider | None = None


def register_extraction_retrieval_provider(provider: ExtractionRetrievalProvider):
    global extraction_retrieval_provider
    extraction_retrieval_provider = provider
