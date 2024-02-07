from django.urls import path
from rest_framework.urlpatterns import format_suffix_patterns

from .views import HelpView, SearchView

urlpatterns = [
    path("", SearchView.as_view(), name="search"),
    path("help", HelpView.as_view(), name="search_help"),
]

urlpatterns = format_suffix_patterns(urlpatterns)
