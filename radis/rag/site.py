from typing import Callable, NamedTuple

from radis.search.site import Search


class RetrievalResult(NamedTuple):
    total_count: int
    coverage: float | None
    document_ids: list[str]


RetrievalHandler = Callable[[Search], RetrievalResult]


class RetrievalProvider(NamedTuple):
    name: str
    handler: RetrievalHandler


retrieval_providers: dict[str, RetrievalProvider] = {}


def register_retrieval_provider(name: str, handler: RetrievalHandler):
    retrieval_providers[name] = RetrievalProvider(name, handler)
