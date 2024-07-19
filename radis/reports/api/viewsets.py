import logging
from typing import Any

from django.db import transaction
from django.http import Http404
from rest_framework import mixins, status, viewsets
from rest_framework.exceptions import MethodNotAllowed
from rest_framework.permissions import IsAdminUser
from rest_framework.request import Request, clone_request
from rest_framework.response import Response
from rest_framework.serializers import BaseSerializer

from ..models import Report
from ..site import (
    document_fetchers,
    reports_created_handlers,
    reports_deleted_handlers,
    reports_updated_handlers,
)
from .serializers import ReportSerializer

logger = logging.getLogger(__name__)


class ReportViewSet(
    mixins.CreateModelMixin,
    mixins.DestroyModelMixin,
    mixins.RetrieveModelMixin,
    mixins.UpdateModelMixin,
    viewsets.GenericViewSet,
):
    """ViewSet for fetch, creating, updating, and deleting Reports.

    Only admins (staff users) can do that.
    """

    serializer_class = ReportSerializer
    queryset = Report.objects.all()
    lookup_field = "document_id"
    permission_classes = [IsAdminUser]

    def get_serializer(self, *args: Any, **kwargs: Any) -> BaseSerializer:
        if isinstance(kwargs.get("data", {}), list):
            kwargs["many"] = True
        return super().get_serializer(*args, **kwargs)

    def retrieve(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        """Retrieve a single Report.

        It also fetches the associated documents from all external databases.
        """
        full = request.GET.get("full", "").lower() in ["true", "1", "yes"]

        instance: Report = self.get_object()
        serializer = self.get_serializer(instance)
        data = serializer.data

        if full:
            documents = {}
            for fetcher in document_fetchers.values():
                document = fetcher.fetch(instance)
                if document:
                    documents[fetcher.source] = document
            data["documents"] = documents

        return Response(data)

    def perform_create(self, serializer: BaseSerializer) -> None:
        super().perform_create(serializer)
        assert serializer.instance
        reports: list[Report] | Report = serializer.instance
        if not isinstance(reports, list):
            reports = [reports]

        def on_commit():
            for handler in reports_created_handlers:
                document_ids = [report.document_id for report in reports]
                logger.debug(f"{handler.name} - handle newly created reports: {document_ids}")
                handler.handle(reports)

        transaction.on_commit(on_commit)

    def update(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        # DRF itself does not support upsert.
        # Workaround adapted from https://gist.github.com/tomchristie/a2ace4577eff2c603b1b
        upsert = request.GET.get("upsert", "").lower() in ["true", "1", "yes"]
        if not upsert:
            return super().update(request, *args, **kwargs)
        else:
            instance = self.get_object_or_none()
            serializer = self.get_serializer(instance, data=request.data)
            serializer.is_valid(raise_exception=True)

            if instance is None:
                self.perform_create(serializer)
                return Response(serializer.data, status=status.HTTP_201_CREATED)

            self.perform_update(serializer)
            return Response(serializer.data)

    def get_object_or_none(self) -> Report | None:
        try:
            return self.get_object()
        except Http404:
            if self.request.method == "PUT":
                self.check_permissions(clone_request(self.request, "POST"))
            else:
                raise

    def perform_update(self, serializer: BaseSerializer) -> None:
        super().perform_update(serializer)
        assert serializer.instance
        reports: list[Report] | Report = serializer.instance
        if not isinstance(reports, list):
            reports = [reports]

        def on_commit():
            for handler in reports_updated_handlers:
                document_ids = [report.document_id for report in reports]
                logger.debug(f"{handler.name} - handle updated reports: {document_ids}")
                handler.handle(reports)

        transaction.on_commit(on_commit)

    def partial_update(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        # Disallow partial updates
        assert request.method
        raise MethodNotAllowed(request.method)

    def perform_destroy(self, instance: Report) -> None:
        super().perform_destroy(instance)

        def on_commit():
            for handler in reports_deleted_handlers:
                logger.debug(f"{handler.name} - handle deleted report: {instance.document_id}")
                handler.handle([instance])

        transaction.on_commit(on_commit)
