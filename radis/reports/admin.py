import logging

from django.contrib import admin, messages
from django.db import transaction
from django.db.models.query import QuerySet
from django.http.request import HttpRequest
from django.http.response import HttpResponse

from .models import Language, Metadata, Modality, Report, ReportsAppSettings
from .site import reports_created_handlers, reports_deleted_handlers, reports_updated_handlers

logger = logging.getLogger(__name__)

admin.site.register(ReportsAppSettings, admin.ModelAdmin)


def show_reindex_warning(request: HttpRequest) -> None:
    messages.warning(request, "Change does not reflect index. Manual reindex required!")


class LanguageAdmin(admin.ModelAdmin):
    def delete_model(self, request: HttpRequest, obj: Language) -> None:
        show_reindex_warning(request)
        return super().delete_model(request, obj)

    def delete_queryset(self, request: HttpRequest, queryset: QuerySet[Language]) -> None:
        show_reindex_warning(request)
        return super().delete_queryset(request, queryset)

    def response_change(self, request: HttpRequest, obj: Language) -> HttpResponse:
        show_reindex_warning(request)
        return super().response_change(request, obj)


admin.site.register(Language, LanguageAdmin)


class ModalityAdmin(admin.ModelAdmin):
    def delete_model(self, request: HttpRequest, obj: Modality) -> None:
        show_reindex_warning(request)
        return super().delete_model(request, obj)

    def delete_queryset(self, request: HttpRequest, queryset: QuerySet[Modality]) -> None:
        show_reindex_warning(request)
        return super().delete_queryset(request, queryset)

    def response_change(self, request: HttpRequest, obj: Modality) -> HttpResponse:
        show_reindex_warning(request)
        return super().response_change(request, obj)


admin.site.register(Modality, ModalityAdmin)


class MetadataInline(admin.TabularInline):
    model = Metadata
    extra = 1
    ordering = ("key",)


class ReportAdmin(admin.ModelAdmin):
    inlines = [MetadataInline]

    def delete_model(self, request: HttpRequest, obj: Report) -> None:
        # Called when deleting a single report (from the admin form view)
        super().delete_model(request, obj)
        logger.debug("Remove in admin deleted report from index: %s", obj)
        transaction.on_commit(
            lambda: [handler.handle([obj]) for handler in reports_deleted_handlers]
        )

    def delete_queryset(self, request: HttpRequest, queryset: QuerySet[Report]) -> None:
        # Called when deleting multiple reports (from the admin list view)
        reports_to_delete = list(queryset)
        print(reports_to_delete)
        super().delete_queryset(request, queryset)
        logger.debug("Remove in admin deleted reports from index: %s", reports_to_delete)
        transaction.on_commit(
            lambda: [handler.handle(reports_to_delete) for handler in reports_deleted_handlers]
        )

    def response_add(
        self, request: HttpRequest, obj: Report, post_url_continue: str | None = None
    ) -> HttpResponse:
        # Called after a new report in the admin is saved (the model itself and also
        # its relations)
        logger.debug("Reindex report added in admin: %s", obj)
        transaction.on_commit(
            lambda: [handler.handle([obj]) for handler in reports_created_handlers]
        )
        return super().response_add(request, obj, post_url_continue)

    def response_change(self, request: HttpRequest, obj: Report) -> HttpResponse:
        # Called after an existing report in the admin is saved (the model itself and also
        # its relations)
        logger.debug("Reindex report changed in admin: %s", obj)
        transaction.on_commit(
            lambda: [handler.handle([obj]) for handler in reports_updated_handlers]
        )
        return super().response_change(request, obj)


admin.site.register(Report, ReportAdmin)
