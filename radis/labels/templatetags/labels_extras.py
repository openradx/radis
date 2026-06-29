from django.template import Library

from ..models import BackfillJob

register = Library()


@register.filter
def backfill_status_css(status: str) -> str:
    css_classes: dict[str, str] = {
        BackfillJob.Status.PENDING: "text-secondary",
        BackfillJob.Status.IN_PROGRESS: "text-info",
        BackfillJob.Status.CANCELED: "text-muted",
        BackfillJob.Status.SUCCESS: "text-success",
        BackfillJob.Status.FAILURE: "text-danger",
    }
    return css_classes.get(status, "")
