from django.urls import path

from .views import VespaAdmin

urlpatterns = [
    path("vespa-admin/", VespaAdmin.as_view(), name="vespa_admin"),
]
