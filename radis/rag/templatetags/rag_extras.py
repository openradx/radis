from typing import Any

from django.template import Library

from radis.rag.models import RagInstance

register = Library()


@register.inclusion_tag("core/_job_detail_control_panel.html", takes_context=True)
def job_control_panel(context: dict[str, Any]) -> dict[str, Any]:
    return {
        "job_delete_url": "rag_job_delete",
        "job_verify_url": "rag_job_verify",
        "job_cancel_url": "rag_job_cancel",
        "job_resume_url": "rag_job_resume",
        "job_retry_url": "rag_job_retry",
        "job_restart_url": "rag_job_restart",
        "user": context["user"],
        "job": context["job"],
    }


@register.inclusion_tag("core/_task_detail_control_panel.html", takes_context=True)
def task_control_panel(context: dict[str, Any]) -> dict[str, Any]:
    return {
        "task_delete_url": "rag_task_delete",
        "task_reset_url": "rag_task_reset",
        "user": context["user"],
        "task": context["task"],
    }


@register.filter
def result_badge_css_class(result: RagInstance.Result) -> str:
    css_classes = {
        RagInstance.Result.ACCEPTED: "text-bg-success",
        RagInstance.Result.REJECTED: "text-bg-danger",
    }
    return css_classes[result]
