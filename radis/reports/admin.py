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


class LabelResultInline(admin.TabularInline):
    model = LabelResult
    extra = 0
    can_delete = False
    readonly_fields = ("label", "value", "generated_at")
    # Inert (read-only inline); see labels.admin.LabelResultAdmin.
    raw_id_fields = ("label",)

    def has_add_permission(self, request: HttpRequest, obj: object = None) -> bool:
        return False


# The confirmation page posts every selected report back as a hidden input, and
# Django rejects a request with more than DATA_UPLOAD_MAX_NUMBER_FIELDS (1,000)
# fields, so a larger selection has to be split before the page is shown.
WITHDRAW_MAX_REPORTS = 500


class WithdrawReportsForm(forms.Form):
    reason = forms.CharField(
        label="Reason for withdrawal",
        widget=forms.Textarea(attrs={"rows": 3}),
    )


class WithdrawnListFilter(admin.SimpleListFilter):
    title = "withdrawn"
    parameter_name = "withdrawn"

    def lookups(self, request: HttpRequest, model_admin: admin.ModelAdmin) -> list[tuple[str, str]]:
        return [("yes", "Yes"), ("no", "No")]

    def queryset(self, request: HttpRequest, queryset: QuerySet[Report]) -> QuerySet[Report]:
        if self.value() == "yes":
            return queryset.filter(withdrawn_at__isnull=False)
        if self.value() == "no":
            return queryset.filter(withdrawn_at__isnull=True)
        return queryset


class ReportAdmin(admin.ModelAdmin):
    inlines = [MetadataInline, LabelResultInline]
    list_display = ("__str__", "is_withdrawn_display")
    list_filter = (WithdrawnListFilter,)
    # Exact, so that the lookup uses the unique index; the default icontains
    # would scan the whole archive.
    search_fields = ("document_id__exact",)
    search_help_text = "Search by exact document ID."
    readonly_fields = ("withdrawn_at", "withdrawn_by", "withdrawal_reason")
    actions = ("withdraw_selected", "restore_selected")

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
        selected = queryset.count()
        if not selected:
            self.message_user(
                request, "The selected reports are already withdrawn.", messages.WARNING
            )
            return None
        if selected > WITHDRAW_MAX_REPORTS:
            self.message_user(
                request,
                f"{selected} reports selected: withdraw at most {WITHDRAW_MAX_REPORTS} at a "
                "time. No report was withdrawn.",
                messages.ERROR,
            )
            return None
        form = WithdrawReportsForm(request.POST if "apply" in request.POST else None)
        if form.is_valid():
            # update(), not save(): the AFTER UPDATE trigger on reports_report
            # mirrors the flag into the search index by itself, and save() would
            # also bump updated_at, which marks the report's labels stale.
            withdrawn = queryset.update(
                withdrawn_at=timezone.now(),
                withdrawn_by=request.user,
                withdrawal_reason=form.cleaned_data["reason"],
            )
            self.message_user(request, f"Withdrew {withdrawn} report(s).", messages.SUCCESS)
            return None
        return TemplateResponse(
            request,
            "admin/reports/report/withdraw_selected_confirmation.html",
            {
                **self.admin_site.each_context(request),
                "title": "Withdraw reports",
                "reports": queryset.only("pk", "document_id"),
                "form": form,
                "opts": self.model._meta,
                "action_checkbox_name": helpers.ACTION_CHECKBOX_NAME,
            },
        )

    @admin.action(description="Restore selected reports", permissions=["change"])
    def restore_selected(self, request: HttpRequest, queryset: QuerySet[Report]) -> None:
        # updated_at moves with the restore because subscriptions select reports
        # by it: a report that was withdrawn while a subscription refreshed is
        # offered to it again. The labeling scan re-labels the report as well.
        restored = queryset.filter(withdrawn_at__isnull=False).update(
            withdrawn_at=None,
            withdrawn_by=None,
            withdrawal_reason="",
            updated_at=timezone.now(),
        )
        if restored:
            self.message_user(request, f"Restored {restored} report(s).", messages.SUCCESS)
        else:
            self.message_user(
                request, "None of the selected reports is withdrawn.", messages.WARNING
            )


admin.site.register(Report, ReportAdmin)
