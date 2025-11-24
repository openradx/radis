from typing import Callable, Iterable, NamedTuple

from radis.search.site import Search, SearchFilters


class SubscriptionFilterProvider(NamedTuple):
    """A class representing a filter provider.

    Attributes:
    - name: The name of the filter provider.
    - count: A function that counts the number of results for a search.
    - filter: A function that returns the document IDs for a filter.
    - max_results: The maximum number of results that can be returned by this provider,
      or None if there is no limit.
    """

    name: str
    filter: Callable[[SearchFilters], Iterable[str]]


subscription_filter_provider: SubscriptionFilterProvider | None = None


def register_subscription_filter_provider(provider: SubscriptionFilterProvider):
    global subscription_filter_provider
    subscription_filter_provider = provider


class SubscriptionRetrievalProvider(NamedTuple):
    """A class representing a retrieval provider.

    Attributes:
    - name: The name of the retrieval provider.
    - retrieve: A function that retrieves the document IDs for a search.
    """

    name: str
    retrieve: Callable[[Search], Iterable[str]]


subscription_retrieval_provider: SubscriptionRetrievalProvider | None = None


def register_subscription_retrieval_provider(provider: SubscriptionRetrievalProvider):
    global subscription_retrieval_provider
    subscription_retrieval_provider = provider
