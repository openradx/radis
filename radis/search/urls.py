from django.urls import path
from rest_framework.urlpatterns import format_suffix_patterns

from .views import SearchView, description_view

urlpatterns = format_suffix_patterns([path("", SearchView.as_view(), name="search")]) + [
    path("description/", description_view, name="search_description"),
]
