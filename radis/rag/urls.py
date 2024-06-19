from adit_radis_shared.common.views import HtmxTemplateView
from django.urls import path

from .views import (
    ChangeAnswerView,
    RagJobCancelView,
    RagJobDeleteView,
    RagJobDetailView,
    RagJobListView,
    RagJobRestartView,
    RagJobResumeView,
    RagJobRetryView,
    RagJobVerifyView,
    RagJobWizardView,
    RagReportInstanceDetailView,
    RagResultListView,
    RagTaskDeleteView,
    RagTaskDetailView,
    RagTaskResetView,
    RagUpdatePreferencesView,
)

urlpatterns = [
    path(
        "update-preferences/",
        RagUpdatePreferencesView.as_view(),
    ),
    path(
        "help/",
        HtmxTemplateView.as_view(template_name="rag/_rag_help.html"),
        name="rag_help",
    ),
    path(
        "jobs/",
        RagJobListView.as_view(),
        name="rag_job_list",
    ),
    path(
        "jobs/new/",
        RagJobWizardView.as_view(),
        name="rag_job_create",
    ),
    path(
        "jobs/<int:pk>/",
        RagJobDetailView.as_view(),
        name="rag_job_detail",
    ),
    path(
        "jobs/<int:pk>/delete/",
        RagJobDeleteView.as_view(),
        name="rag_job_delete",
    ),
    path(
        "jobs/<int:pk>/verify/",
        RagJobVerifyView.as_view(),
        name="rag_job_verify",
    ),
    path(
        "jobs/<int:pk>/cancel/",
        RagJobCancelView.as_view(),
        name="rag_job_cancel",
    ),
    path(
        "jobs/<int:pk>/resume/",
        RagJobResumeView.as_view(),
        name="rag_job_resume",
    ),
    path(
        "jobs/<int:pk>/retry/",
        RagJobRetryView.as_view(),
        name="rag_job_retry",
    ),
    path(
        "jobs/<int:pk>/restart/",
        RagJobRestartView.as_view(),
        name="rag_job_restart",
    ),
    path(
        "jobs/<int:pk>/verify/",
        RagJobVerifyView.as_view(),
        name="rag_job_verify",
    ),
    path(
        "tasks/<int:pk>/",
        RagTaskDetailView.as_view(),
        name="rag_task_detail",
    ),
    path(
        "tasks/<int:pk>/delete/",
        RagTaskDeleteView.as_view(),
        name="rag_task_delete",
    ),
    path(
        "tasks/<int:pk>/reset/",
        RagTaskResetView.as_view(),
        name="rag_task_reset",
    ),
    path(
        "jobs/<int:pk>/results/",
        RagResultListView.as_view(),
        name="rag_result_list",
    ),
    path(
        "change-answer/<int:result_id>/",
        ChangeAnswerView.as_view(),
        name="rag_change_answer",
    ),
    path(
        "tasks/<int:task_id>/report-instances/<int:pk>/",
        RagReportInstanceDetailView.as_view(),
        name="rag_report_instance_detail",
    ),
]
