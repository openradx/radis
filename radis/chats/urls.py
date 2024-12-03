from django.urls import path
from rest_framework.urlpatterns import format_suffix_patterns

from .views import (
    chat_clear_all,
    chat_create_view,
    chat_delete_view,
    chat_detail_view,
    chat_list_view,
    chat_update_view,
)

urlpatterns = [
    path("", chat_list_view, name="chat_list"),
    path("clear/", chat_clear_all, name="chat_clear_all"),
    path("create/", chat_create_view, name="chat_create"),
    path("<int:pk>/", chat_detail_view, name="chat_detail"),
    path("<int:pk>/update/", chat_update_view, name="chat_update"),
    path("<int:pk>/delete/", chat_delete_view, name="chat_delete"),
]

urlpatterns = format_suffix_patterns(urlpatterns)
