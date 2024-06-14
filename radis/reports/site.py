from typing import Any, Callable, NamedTuple

from django.http import HttpRequest

from .models import Report


class ReportsCreatedHandler(NamedTuple):
    name: str
    handle: Callable[[list[Report]], None]


reports_created_handlers: list[ReportsCreatedHandler] = []


def register_reports_created_handler(handler: ReportsCreatedHandler) -> None:
    """Register a handler for when reports are created in the PostgreSQL database.

    The handler can be used to sync resp. index those reports in a search database like Vespa.
    """
    reports_created_handlers.append(handler)


class ReportsUpdatedHandler(NamedTuple):
    name: str
    handle: Callable[[list[Report]], None]


reports_updated_handlers: list[ReportsUpdatedHandler] = []


def register_reports_updated_handler(handler: ReportsUpdatedHandler) -> None:
    """Register a handler for when reports are updated in the PostgreSQL database.

    The handler can be used to sync resp. re-index those reports in a search database like Vespa.
    """
    reports_updated_handlers.append(handler)


class ReportsDeletedHandler(NamedTuple):
    name: str
    handle: Callable[[list[Report]], None]


reports_deleted_handlers: list[ReportsDeletedHandler] = []


def register_reports_deleted_handler(handler: ReportsDeletedHandler) -> None:
    """Register a handler for when reports are deleted in the PostgreSQL database.

    The handler can be used to remove those reports from the index of search databases like Vespa.
    """
    reports_deleted_handlers.append(handler)


FetchDocument = Callable[[Report], dict[str, Any] | None]


class DocumentFetcher(NamedTuple):
    source: str
    fetch: FetchDocument


document_fetchers: dict[str, DocumentFetcher] = {}


def register_document_fetcher(source: str, fetch: FetchDocument) -> None:
    """Register a document fetcher.

    A document fetcher is a function that takes a report from the PostgreSQL
    database and returns a document in the form of a dictionary from another
    database (like Vespa).
    """
    document_fetchers[source] = DocumentFetcher(source, fetch)


class ReportPanelButton(NamedTuple):
    order: int
    template_name: str


report_panel_buttons: list[ReportPanelButton] = []


def register_report_panel_button(order: int, template_name: str) -> None:
    """Register an additional button for the panel below each report."""
    global report_panel_buttons
    report_panel_buttons.append(ReportPanelButton(order, template_name))
    report_panel_buttons = sorted(report_panel_buttons, key=lambda x: x.order)


def base_context_processor(request: HttpRequest) -> dict[str, Any]:
    return {
        "report_panel_buttons": report_panel_buttons,
    }
