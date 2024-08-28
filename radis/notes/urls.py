from django.urls import path

from radis.notes.views import NoteAvailableBadgeView, NoteDetailView, NoteEditView, NoteListView

urlpatterns = [
    path("", NoteListView.as_view(), name="note_list"),
    path("<int:pk>/", NoteDetailView.as_view(), name="note_detail"),
    path("edit/<int:report_id>/", NoteEditView.as_view(), name="note_edit"),
    path(
        "available-badge/<int:report_id>/",
        NoteAvailableBadgeView.as_view(),
        name="note_available_badge",
    ),
]
