from typing import Callable, NamedTuple

from .models import SearchResult


class Search(NamedTuple):
    query: str
    offset: int = 0
    page_size: int = 10


Searcher = Callable[[Search], SearchResult]


class SearchHandler(NamedTuple):
    name: str
    searcher: Searcher
    template_name: str


search_handlers: dict[str, SearchHandler] = {}


def register_search_handler(
    name: str,  # TODO: may rename to algorithm
    searcher: Searcher,
    template_name: str,
) -> None:
    """Register a search handler.

    The name can be selected by the user in the search form. The searcher is called
    when the user submits the form and returns the results. The template name is
    the partial to be rendered as help below the search form.
    """
    search_handlers[name] = SearchHandler(name, searcher, template_name)
