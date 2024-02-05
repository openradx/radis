from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .viewsets import ReportViewSet

router = DefaultRouter()
router.register(r"", ReportViewSet)

urlpatterns = [
    path("", include(router.urls)),
]
