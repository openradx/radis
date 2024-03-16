import logging
import re
from datetime import date, datetime, time
from typing import Any

from django.conf import settings
from django.template import Library
from django.template.defaultfilters import join

from ..models import AnalysisJob, AnalysisTask

logger = logging.getLogger(__name__)

register = Library()


@register.inclusion_tag("core/_bootstrap_icon.html")
def bootstrap_icon(icon_name: str, size: int = 16):
    return {"icon_name": icon_name, "size": size}


@register.filter
def access_item(d: dict, key: str) -> Any:
    return d.get(key, "")


@register.simple_tag(takes_context=True)
def url_replace(context: dict[str, Any], field: str, value: Any) -> str:
    dict_ = context["request"].GET.copy()
    dict_[field] = value
    return dict_.urlencode()


@register.simple_tag
def filter_modalities(modalities: list[str]) -> list[str]:
    exclude_modalities = settings.EXCLUDED_MODALITIES
    return [modality for modality in modalities if modality not in exclude_modalities]


@register.filter(is_safe=True, needs_autoescape=True)
def join_if_list(value: Any, arg: str, autoescape=True) -> Any:
    if isinstance(value, list):
        return join(value, arg, autoescape)

    return value


@register.simple_tag
def combine_datetime(date: date, time: time) -> datetime:
    return datetime.combine(date, time)


@register.filter
def alert_class(tag: str) -> str:
    tag_map = {
        "info": "alert-info",
        "success": "alert-success",
        "warning": "alert-warning",
        "error": "alert-danger",
    }
    return tag_map.get(tag, "alert-secondary")


@register.filter
def message_symbol(tag: str) -> str:
    tag_map = {
        "info": "info",
        "success": "success",
        "warning": "warning",
        "error": "error",
    }
    return tag_map.get(tag, "bug")


@register.filter
def analysis_job_status_css_class(status: AnalysisJob.Status) -> str:
    css_classes = {
        AnalysisJob.Status.UNVERIFIED: "text-info",
        AnalysisJob.Status.PREPARING: "text-info",
        AnalysisJob.Status.PENDING: "text-secondary",
        AnalysisJob.Status.IN_PROGRESS: "text-info",
        AnalysisJob.Status.CANCELING: "text-muted",
        AnalysisJob.Status.CANCELED: "text-muted",
        AnalysisJob.Status.SUCCESS: "text-success",
        AnalysisJob.Status.WARNING: "text-warning",
        AnalysisJob.Status.FAILURE: "text-danger",
    }
    return css_classes[status]


@register.filter
def analysis_task_status_css_class(status: AnalysisTask.Status) -> str:
    css_classes = {
        AnalysisTask.Status.PENDING: "text-secondary",
        AnalysisTask.Status.IN_PROGRESS: "text-info",
        AnalysisTask.Status.CANCELED: "text-muted",
        AnalysisTask.Status.SUCCESS: "text-success",
        AnalysisTask.Status.WARNING: "text-warning",
        AnalysisTask.Status.FAILURE: "text-danger",
    }
    return css_classes[status]


# TODO: Resolve reference names from another source in the context
# Context must be set in the view
@register.simple_tag(takes_context=True)
def url_abbreviation(context: dict, url: str):
    abbr = re.sub(r"^(https?://)?(www.)?", "", url)
    return abbr[:5]
