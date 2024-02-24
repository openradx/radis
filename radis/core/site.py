from typing import Any, Callable, Literal, NamedTuple

from django.conf import settings
from django.http import HttpRequest
from django.middleware.csrf import get_token

from .models import Report


class MainMenuItem(NamedTuple):
    url_name: str
    label: str


main_menu_items: list[MainMenuItem] = []


def register_main_menu_item(url_name: str, label: str) -> None:
    main_menu_items.append(MainMenuItem(url_name, label))


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
    from .utils.auth_utils import is_logged_in_user

    theme = "auto"
    theme_color = "light"
    user = request.user
    if is_logged_in_user(user):
        preferences = user.preferences
        theme = preferences.get("theme", theme)
        theme_color = preferences.get("theme_color", theme_color)

    return {
        "version": settings.RADIS_VERSION,
        "base_url": settings.BASE_URL,
        "support_email": settings.SUPPORT_EMAIL,
        "main_menu_items": main_menu_items,
        "theme": theme,
        "theme_color": theme_color,
        "report_panel_buttons": report_panel_buttons,
        # Variables in public are also available on the client via JavaScript,
        # see base_generic.html
        "public": {
            "debug": settings.DEBUG,
            "csrf_token": get_token(request),
        },
    }
