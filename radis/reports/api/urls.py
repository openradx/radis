from adrf.routers import DefaultRouter
from django.urls import include, path

from .viewsets import ReportViewSet

router = DefaultRouter()
router.register("", ReportViewSet, basename="report")

urlpatterns = [
    path("", include(router.urls)),
]
