from django.urls import path
from rest_framework.urlpatterns import format_suffix_patterns

from .views import ReportBodyView, ReportChatView, ReportDetailView

urlpatterns = [
    path("<int:pk>/body/", ReportBodyView.as_view(), name="report_body"),
    path("<int:pk>/chat/", ReportChatView.as_view(), name="report_chat"),
    path("<int:pk>/", ReportDetailView.as_view(), name="report_detail"),
]

urlpatterns = format_suffix_patterns(urlpatterns)
