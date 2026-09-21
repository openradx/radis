import logging

from django import forms
from django.contrib import admin, messages
from django.contrib.admin import helpers
from django.db import transaction
from django.db.models.query import QuerySet
from django.http.request import HttpRequest
from django.http.response import HttpResponse
from django.template.response import TemplateResponse
from django.utils import timezone

from radis.labels.models import LabelResult

from .models import Language, Metadata, Modality, Report, ReportsAppSettings, WithdrawnReport
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


class LabelResultInline(admin.TabularInline):
    model = LabelResult
    extra = 0
    can_delete = False
    readonly_fields = ("label", "value", "generated_at")
    # Inert (read-only inline); see labels.admin.LabelResultAdmin.
    raw_id_fields = ("label",)

    def has_add_permission(self, request: HttpRequest, obj: object = None) -> bool:
        return False


class WithdrawReportsForm(forms.Form):
    reason = forms.CharField(
        label="Reason for withdrawal",
        widget=forms.Textarea(attrs={"rows": 3}),
    )


class ReportAdmin(admin.ModelAdmin):
    inlines = [MetadataInline, LabelResultInline]
    list_display = ("__str__", "is_withdrawn_display")
    list_filter = (("withdrawn_at", admin.EmptyFieldListFilter),)
    readonly_fields = ("withdrawn_at", "withdrawn_by", "withdrawal_reason")
    actions = ("withdraw_selected",)

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

    @admin.display(boolean=True, description="Withdrawn")
    def is_withdrawn_display(self, obj: Report) -> bool:
        return obj.is_withdrawn

    @admin.action(description="Withdraw selected reports", permissions=["change"])
    def withdraw_selected(
        self, request: HttpRequest, queryset: QuerySet[Report]
    ) -> HttpResponse | None:
        queryset = queryset.filter(withdrawn_at__isnull=True)
        form = WithdrawReportsForm(request.POST if "apply" in request.POST else None)
        if "apply" in request.POST and form.is_valid():
            reason = form.cleaned_data["reason"]
            reports = list(queryset)
            # update() on purpose: the projection trigger is the only sync a
            # state flip needs; skipping post_save keeps re-embedding and
            # label staleness out of it.
            queryset.update(
                withdrawn_at=timezone.now(),
                withdrawn_by=request.user,
                withdrawal_reason=reason,
            )
            for report in reports:
                self.log_change(request, report, f"Withdrawn: {reason}")
            self.message_user(request, f"Withdrew {len(reports)} report(s).", messages.SUCCESS)
            return None
        return TemplateResponse(
            request,
            "admin/reports/report/withdraw_selected_confirmation.html",
            {
                **self.admin_site.each_context(request),
                "title": "Withdraw reports",
                "queryset": queryset,
                "form": form,
                "opts": self.model._meta,
                "action_checkbox_name": helpers.ACTION_CHECKBOX_NAME,
            },
        )


admin.site.register(Report, ReportAdmin)


class WithdrawnReportAdmin(ReportAdmin):
    """Inherits ReportAdmin so hard delete keeps firing the index-cleanup
    handlers and the change form keeps its read-only withdrawal fields."""

    list_display = (
        "document_id",
        "patient_id",
        "study_description",
        "withdrawn_at",
        "withdrawn_by",
        "withdrawal_reason",
    )
    list_filter = ()
    actions = ("restore_selected",)

    def get_queryset(self, request: HttpRequest) -> QuerySet[Report]:
        return super().get_queryset(request).filter(withdrawn_at__isnull=False)

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    @admin.action(description="Restore selected reports", permissions=["change"])
    def restore_selected(self, request: HttpRequest, queryset: QuerySet[Report]) -> None:
        report_ids = list(queryset.values_list("pk", flat=True))
        # update() for the same reason as withdraw_selected: the projection
        # trigger is the only sync a state flip needs.
        queryset.update(withdrawn_at=None, withdrawn_by=None, withdrawal_reason="")
        # Log against the concrete model: an entry stamped with the proxy's
        # content type never surfaces in the report's admin history.
        for report in Report.objects.filter(pk__in=report_ids):
            self.log_change(request, report, "Restored from withdrawal")
        self.message_user(request, f"Restored {len(report_ids)} report(s).", messages.SUCCESS)


admin.site.register(WithdrawnReport, WithdrawnReportAdmin)
