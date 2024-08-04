from adit_radis_shared.common.views import HtmxTemplateView
from django.urls import path

from .views import (InboxCreateView, InboxDeleteView, InboxDetailView,
                    InboxListView)

urlpatterns = [
    path(
        "create/",
        InboxCreateView.as_view(),
        name="inbox_create"
    ),
    path(
        "list/",
        InboxListView.as_view(),
        name="inbox_list"
    ),
    path(
        "<int:pk>/",
        InboxDetailView.as_view(),
        name="inbox_detail"
    ),
    path(
        "<int:pk>/delete/",
        InboxDeleteView.as_view(),
        name="inbox_delete"
    ),
    path(
        "help/",
        HtmxTemplateView.as_view(template_name="inbox/_inbox_help.html"),
        name="inbox_help",
    ),
]
