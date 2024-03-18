from typing import Callable, NamedTuple

from radis.search.site import Search


class RetrievalResult(NamedTuple):
    total_count: int
    coverage: float | None
    document_ids: list[str]


RetrievalHandler = Callable[[Search], RetrievalResult]


class RetrievalProvider(NamedTuple):
    """
    A class representing a retrieval provider.

    Attributes:
    - name (str): The name of the retrieval provider.
    - handler (SearchHandler): The function that handles the retrieval.
    - max_results (int): The maximum number of results that can be returned.
      Must be smaller than offset + limit when searching.
    - info_template (str): The template to be rendered as info.
    """

    name: str
    handler: RetrievalHandler
    max_results: int
    info_template: str


retrieval_providers: dict[str, RetrievalProvider] = {}


def register_retrieval_provider(retrieval_provider: RetrievalProvider):
    retrieval_providers[retrieval_provider.name] = retrieval_provider
