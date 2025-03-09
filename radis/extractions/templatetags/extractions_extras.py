from typing import Any

from django.template import Library

from radis.extractions.models import OutputType

register = Library()


@register.inclusion_tag("core/_job_detail_control_panel.html", takes_context=True)
def job_control_panel(context: dict[str, Any]) -> dict[str, Any]:
    return {
        "job_delete_url": "extraction_job_delete",
        "job_verify_url": "extraction_job_verify",
        "job_cancel_url": "extraction_job_cancel",
        "job_resume_url": "extraction_job_resume",
        "job_retry_url": "extraction_job_retry",
        "job_restart_url": "extraction_job_restart",
        "user": context["user"],
        "job": context["job"],
    }


@register.inclusion_tag("core/_task_detail_control_panel.html", takes_context=True)
def task_control_panel(context: dict[str, Any]) -> dict[str, Any]:
    return {
        "task_delete_url": "extraction_task_delete",
        "task_reset_url": "extraction_task_reset",
        "user": context["user"],
        "task": context["task"],
    }


@register.filter
def human_readable_output_type(output_type: str) -> str:
    return OutputType(output_type).label
