from django.urls import path

from . import views

urlpatterns = [
    path("", views.QuestionSetListView.as_view(), name="question_set_list"),
    path("create/", views.QuestionSetCreateView.as_view(), name="question_set_create"),
    path("<int:pk>/", views.QuestionSetDetailView.as_view(), name="question_set_detail"),
    path(
        "<int:pk>/update/",
        views.QuestionSetUpdateView.as_view(),
        name="question_set_update",
    ),
    path(
        "<int:pk>/delete/",
        views.QuestionSetDeleteView.as_view(),
        name="question_set_delete",
    ),
    path(
        "<int:question_set_pk>/questions/create/",
        views.QuestionCreateView.as_view(),
        name="question_create",
    ),
    path(
        "<int:question_set_pk>/questions/<int:pk>/update/",
        views.QuestionUpdateView.as_view(),
        name="question_update",
    ),
    path(
        "<int:question_set_pk>/questions/<int:pk>/delete/",
        views.QuestionDeleteView.as_view(),
        name="question_delete",
    ),
    path(
        "backfill/<int:pk>/cancel/",
        views.BackfillCancelView.as_view(),
        name="backfill_cancel",
    ),
    path(
        "backfill/<int:pk>/retry/",
        views.BackfillRetryView.as_view(),
        name="backfill_retry",
    ),
    path(
        "<int:pk>/backfill/launch/",
        views.BackfillLaunchView.as_view(),
        name="backfill_launch",
    ),
    # --- Developer-only evaluation routes ---
    # These are part of the evaluation harness used to validate prompts and
    # model choices (see radis/labels/utils/eval_metrics.py and the
    # labels_eval_seed / labels_eval_report management commands). They are
    # intentionally not surfaced in the main navigation and are reachable
    # only by URL.
    path(
        "<int:pk>/eval/",
        views.QuestionSetEvalView.as_view(),
        name="question_set_eval",
    ),
    path(
        "eval/<int:pk>/",
        views.EvalSampleDetailView.as_view(),
        name="eval_sample_detail",
    ),
]
