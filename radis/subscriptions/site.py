from typing import Callable, Iterable, NamedTuple

from radis.search.site import SearchFilters


class FilterProvider(NamedTuple):
    """
    A class representing a filter provider.

    Attributes:
    - name (str): The name of the filter provider.
    - count (Callable[[RagSearch], int]): A function that counts the number of results for a search.
    - filter (Callable[[RagSearch], Iterable[str]]): A function that returns the document IDs
      for a filter.
    - max_results (int | None): The maximum number of results that can be returned by this
      provider, or None if there is no limit.
    """

    name: str
    filter: Callable[[SearchFilters], Iterable[str]]
    max_results: int | None


filter_providers: dict[str, FilterProvider] = {}


def register_filter_provider(filter_provider: FilterProvider):
    filter_providers[filter_provider.name] = filter_provider
