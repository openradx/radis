from typing import Callable, Iterable, NamedTuple

from radis.search.site import Search


class RetrievalProvider(NamedTuple):
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


retrieval_providers: dict[str, RetrievalProvider] = {}


def register_retrieval_provider(retrieval_provider: RetrievalProvider):
    retrieval_providers[retrieval_provider.name] = retrieval_provider
