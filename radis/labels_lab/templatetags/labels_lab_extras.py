from django.template import Library

from radis.labels.models import LabelResult

from ..questions import NOT_SURFACED

register = Library()

_VALUE_COLORS = {
    "YES": "success",
    "NO": "secondary",
    "PRESENT": "danger",
    "LIKELY": "warning",
    "POSSIBLE": "info",
    "ABSENT": "secondary",
    "UNMENTIONED": "light",
    NOT_SURFACED: "secondary",
}


@register.filter
def value_color(value: object) -> str:
    """The Bootstrap theme color a gate answer or bucket is shown in."""
    return _VALUE_COLORS.get(str(value), "light")


@register.simple_tag
def bucket_values() -> list[str]:
    return LabelResult.Value.values
