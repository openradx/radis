from django.urls import path
from rest_framework.urlpatterns import format_suffix_patterns

from .views import ReportBodyView, ReportDetailView, ReportListView, ReportOverviewView

urlpatterns = [
    path("overview/", ReportOverviewView.as_view(), name="report_overview"),
    path("", ReportListView.as_view(), name="report_list"),
    path("<int:pk>/body/", ReportBodyView.as_view(), name="report_body"),
    path("<int:pk>/", ReportDetailView.as_view(), name="report_detail"),
]

urlpatterns = format_suffix_patterns(urlpatterns)
