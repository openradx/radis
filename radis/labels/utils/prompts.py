from collections.abc import Sequence
from string import Template

from django.conf import settings


def format_label_definitions(labels: Sequence) -> str:
    # One line per label, so a multi-line description cannot be read as a new label.
    return "\n".join(f"- {lbl.name}: {' '.join(lbl.description.split())}" for lbl in labels)


def render_label_prompt(report_body: str, labels: Sequence) -> str:
    return Template(settings.LABELING_SYSTEM_PROMPT).substitute(
        report=report_body, labels=format_label_definitions(labels)
    )


def render_gate_prompt(report_body: str) -> str:
    return Template(settings.LABELING_GATE_SYSTEM_PROMPT).substitute(report=report_body)
