from django.urls import path
from rest_framework.urlpatterns import format_suffix_patterns

from .views import InfoView, SearchView

urlpatterns = [
    path("", SearchView.as_view(), name="search"),
    path("info", InfoView.as_view(), name="search_info"),
]

urlpatterns = format_suffix_patterns(urlpatterns)
