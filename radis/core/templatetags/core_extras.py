import logging
import re

from django.template import Library

from ..models import AnalysisJob, AnalysisTask

logger = logging.getLogger(__name__)

register = Library()


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


@register.simple_tag
def url_abbreviation(url: str):
    abbr = re.sub(r"^(https?://)?(www.)?", "", url)
    return abbr[:5]
