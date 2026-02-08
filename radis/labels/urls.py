from django.urls import path

from . import views

urlpatterns = [
    path("", views.LabelGroupListView.as_view(), name="label_group_list"),
    path("create/", views.LabelGroupCreateView.as_view(), name="label_group_create"),
    path("<int:pk>/", views.LabelGroupDetailView.as_view(), name="label_group_detail"),
    path(
        "<int:pk>/update/",
        views.LabelGroupUpdateView.as_view(),
        name="label_group_update",
    ),
    path(
        "<int:pk>/delete/",
        views.LabelGroupDeleteView.as_view(),
        name="label_group_delete",
    ),
    path(
        "<int:group_pk>/questions/create/",
        views.LabelQuestionCreateView.as_view(),
        name="label_question_create",
    ),
    path(
        "<int:group_pk>/questions/<int:pk>/update/",
        views.LabelQuestionUpdateView.as_view(),
        name="label_question_update",
    ),
    path(
        "<int:group_pk>/questions/<int:pk>/delete/",
        views.LabelQuestionDeleteView.as_view(),
        name="label_question_delete",
    ),
]
