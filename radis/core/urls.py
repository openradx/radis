from django.urls import path

from .views import (
    BroadcastView,
    FlowerProxyView,
    HomeView,
    ReportDetailView,
    ReportPreviewView,
    UpdatePreferencesView,
    admin_section,
)

urlpatterns = [
    path("update-preferences/", UpdatePreferencesView.as_view()),
    path("admin-section/", admin_section, name="admin_section"),
    path("admin-section/broadcast/", BroadcastView.as_view(), name="broadcast"),
    path("", HomeView.as_view(), name="home"),
    path("reports/<int:pk>/preview/", ReportPreviewView.as_view(), name="report_preview"),
    path("reports/<int:pk>/", ReportDetailView.as_view(), name="report_detail"),
    FlowerProxyView.as_url(),
]
