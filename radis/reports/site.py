from typing import Any, NamedTuple

from django.http import HttpRequest


class ReportPanelButton(NamedTuple):
    order: int
    template_name: str


report_panel_buttons: list[ReportPanelButton] = []


def register_report_panel_button(order: int, template_name: str) -> None:
    global report_panel_buttons
    report_panel_buttons.append(ReportPanelButton(order, template_name))
    report_panel_buttons = sorted(report_panel_buttons, key=lambda x: x.order)


def base_context_processor(request: HttpRequest) -> dict[str, Any]:
    return {
        "report_panel_buttons": report_panel_buttons,
    }
