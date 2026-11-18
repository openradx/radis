from string import Template

from django.apps import AppConfig
from django.conf import settings
from django.core.checks import Error, register


class LabelsConfig(AppConfig):
    name = "radis.labels"
    verbose_name = "Labels"


@register()
def check_labeling_prompt_placeholders(app_configs, **kwargs):
    """Fail loudly when LABELING_SYSTEM_PROMPT lacks $labels or $report.

    The prompt is the only place the model reads the label definitions and the report;
    an override without either placeholder would label blind without any error.
    """
    present = set(Template(settings.LABELING_SYSTEM_PROMPT).get_identifiers())
    missing = sorted({"labels", "report"} - present)
    if not missing:
        return []
    placeholders = ", ".join(f"${name}" for name in missing)
    return [
        Error(
            f"LABELING_SYSTEM_PROMPT is missing the placeholder(s) {placeholders}.",
            id="labels.E001",
            hint=(
                "Add them to LABELING_SYSTEM_PROMPT in your .env, or unset the variable "
                "to use the built-in default."
            ),
        )
    ]
