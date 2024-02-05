from typing import Any, Callable, Literal, NamedTuple

from radis.reports.models import Report

ReportEventType = Literal["created", "updated", "deleted"]
ReportEventHandler = Callable[[ReportEventType, Report], None]

report_event_handlers: list[ReportEventHandler] = []


def register_report_handler(handler: ReportEventHandler) -> None:
    """Register a report event handler.

    The report handler gets notified a report is created, updated, or deleted in
    PostgreSQL database. It can be used to sync report documents in other
    databases like Vespa.
    """
    report_event_handlers.append(handler)


FetchDocument = Callable[[Report], dict[str, Any] | None]


class DocumentFetcher(NamedTuple):
    source: str
    fetch: FetchDocument


document_fetchers: list[DocumentFetcher] = []


def register_document_fetcher(source: str, fetch: FetchDocument) -> None:
    """Register a document fetcher.

    A document fetcher is a function that takes a report from the PostgreSQL
    database and returns a document in the form of a dictionary from another
    database (like Vespa).
    """
    document_fetchers.append(DocumentFetcher(source, fetch))
