from string import Template

from django.conf import settings


def render_label_prompt(report_body: str) -> str:
    return Template(settings.LABELING_SYSTEM_PROMPT).substitute(report=report_body)


def render_gate_prompt(report_body: str) -> str:
    return Template(settings.LABELING_GATE_SYSTEM_PROMPT).substitute(report=report_body)
