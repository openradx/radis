from adit_radis_shared.common.views import BroadcastView
from django.urls import path

from .views import (
    HomeView,
    UpdatePreferencesView,
    admin_section,
)

urlpatterns = [
    path(
        "update-preferences/",
        UpdatePreferencesView.as_view(),
    ),
    path(
        "admin-section/",
        admin_section,
        name="admin_section",
    ),
    path(
        "admin-section/broadcast/",
        BroadcastView.as_view(),
        name="broadcast",
    ),
    path(
        "",
        HomeView.as_view(),
        name="home",
    ),
]
