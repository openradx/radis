from django.template import Library

from ..models import LabelBackfillJob

register = Library()


@register.filter
def backfill_status_css(status: str) -> str:
    css_classes = {
        LabelBackfillJob.Status.PENDING: "text-secondary",
        LabelBackfillJob.Status.IN_PROGRESS: "text-info",
        LabelBackfillJob.Status.CANCELING: "text-muted",
        LabelBackfillJob.Status.CANCELED: "text-muted",
        LabelBackfillJob.Status.SUCCESS: "text-success",
        LabelBackfillJob.Status.FAILURE: "text-danger",
    }
    return css_classes.get(status, "")
