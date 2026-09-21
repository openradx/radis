from collections.abc import Callable, Iterable
from typing import NamedTuple

from radis.search.site import SearchFilters


class SubscriptionFilterProvider(NamedTuple):
    """A class representing a filter provider.

    Attributes:
    - name: The name of the filter provider.
    - filter: A function that returns the document IDs matching the filters.
    """

    name: str
    filter: Callable[[SearchFilters], Iterable[str]]


subscription_filter_provider: SubscriptionFilterProvider | None = None


def register_subscription_filter_provider(provider: SubscriptionFilterProvider):
    global subscription_filter_provider
    subscription_filter_provider = provider
