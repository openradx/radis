from django.urls import path

from .views import GenerateReportView

app_name = "report_generator"

urlpatterns = [
    path("", GenerateReportView.as_view(), name="generate"),
]
