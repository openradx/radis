from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .viewsets import ReportViewSet

router = DefaultRouter()
router.register("", ReportViewSet, basename="report")

urlpatterns = [
    path("", include(router.urls)),
]
