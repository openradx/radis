from typing import Callable, Iterable, NamedTuple

from radis.search.site import Search


class RetrievalProvider(NamedTuple):
    """
    A class representing a retrieval provider.

    Attributes:
    - name (str): The name of the retrieval provider.
    - count (Callable[[RagSearch], int]): A function that counts the number of results for a search.
    - retrieve (Callable[[RagSearch], Iterable[str]]): A function that retrieves the document IDs
      for a search.
    - max_results (int | None): The maximum number of results that can be retrieved by this
      provider, or None if there is no limit.
    - info_template (str): The template to be rendered as info.
    """

    name: str
    count: Callable[[Search], int]
    retrieve: Callable[[Search], Iterable[str]]
    max_results: int | None
    info_template: str


retrieval_providers: dict[str, RetrievalProvider] = {}


def register_retrieval_provider(retrieval_provider: RetrievalProvider):
    retrieval_providers[retrieval_provider.name] = retrieval_provider
