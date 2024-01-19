from django.urls import path
from rest_framework.urlpatterns import format_suffix_patterns

from .views import RagsearchView

urlpatterns = [
    path("", RagsearchView.as_view(), name="ragsearch"),
]

urlpatterns = format_suffix_patterns(urlpatterns)
