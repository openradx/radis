from django.urls import path, re_path

from .views import (
    ReportBulkUpsertAPIView,
    ReportDetailAPIView,
    ReportListAPIView,
)

urlpatterns = [
    path("", ReportListAPIView.as_view(), name="report-list"),
    path("bulk-upsert/", ReportBulkUpsertAPIView.as_view(), name="report-bulk-upsert"),
    # Regex matches DRF DefaultRouter's default lookup pattern ([^/.]+), preserving
    # the legacy contract that document_id may not contain "." or "/".
    re_path(
        r"^(?P<document_id>[^/.]+)/$",
        ReportDetailAPIView.as_view(),
        name="report-detail",
    ),
]
