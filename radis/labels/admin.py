from django.contrib import admin, messages
from django.db import IntegrityError, transaction
from django.http import HttpRequest, HttpResponseRedirect
from django.urls import path, reverse

from radis.core.utils.model_utils import cancel_job

from .models import (
    GateAnswer,
    Label,
    LabelGroup,
    LabelingJob,
    LabelingScanCheckpoint,
    LabelingTask,
    LabelResult,
)


@admin.register(LabelGroup)
class LabelGroupAdmin(admin.ModelAdmin):
    list_display = ("name", "gate_question", "updated_at")
    search_fields = ("name",)  # required for LabelAdmin autocomplete
    ordering = ("name",)
    readonly_fields = ("updated_at",)


@admin.register(Label)
class LabelAdmin(admin.ModelAdmin):
    autocomplete_fields = ["group"]
    list_display = ("name", "group", "active", "updated_at")
    list_filter = ("active", "group")
    search_fields = ("name", "group__name", "description")
    ordering = ("group__name", "name")
    readonly_fields = ("created_at", "updated_at")


class _ReadOnlyAdmin(admin.ModelAdmin):
    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(self, request: HttpRequest, obj: object = None) -> bool:
        return False

    def has_delete_permission(self, request: HttpRequest, obj: object = None) -> bool:
        return False


@admin.register(LabelResult)
class LabelResultAdmin(_ReadOnlyAdmin):
    list_display = ("report", "label", "value", "is_stale", "generated_at")
    list_filter = ("value", "label")
    search_fields = ("report__document_id", "label__name")
    # Inert while this admin is read-only (read-only fields never render as editable widgets),
    # but kept as a safety net: if the read-only guard is ever loosened, these degrade to ID
    # inputs instead of a <select> dropdown over the huge Report table.
    raw_id_fields = ("report", "label")

    @admin.display(boolean=True, description="Stale")
    def is_stale(self, obj: LabelResult) -> bool:
        return obj.generated_at < obj.label.updated_at


@admin.register(GateAnswer)
class GateAnswerAdmin(_ReadOnlyAdmin):
    list_display = ("report", "label_group", "value", "is_stale", "generated_at")
    list_filter = ("value", "label_group")
    search_fields = ("report__document_id", "label_group__name")
    # See LabelResultAdmin: inert under read-only, kept so a future editable admin degrades to
    # ID inputs instead of a dropdown over the huge Report table.
    raw_id_fields = ("report", "label_group")

    @admin.display(boolean=True, description="Stale")
    def is_stale(self, obj: GateAnswer) -> bool:
        return obj.generated_at < obj.label_group.updated_at


@admin.register(LabelingScanCheckpoint)
class LabelingScanCheckpointAdmin(admin.ModelAdmin):
    list_display = ("last_scanned_at",)
    readonly_fields = ("last_scanned_at",)

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(self, request: HttpRequest, obj: object = None) -> bool:
        return False

    def has_delete_permission(self, request: HttpRequest, obj: object = None) -> bool:
        return False


@admin.register(LabelingJob)
class LabelingJobAdmin(admin.ModelAdmin):
    # Uses a custom changelist template that adds a "Run backfill now" button so that
    # the action does not require selecting a row (Django's built-in action mechanism
    # enforces at least one selected object).
    change_list_template = "admin/labels/labelingjob/change_list.html"
    # Adds a "Cancel job" button to the read-only detail page when the job is cancelable.
    change_form_template = "admin/labels/labelingjob/change_form.html"

    list_display = ("id", "trigger", "status", "owner", "created_at", "ended_at")
    list_filter = ("trigger", "status")
    # The job detail page is a read-only monitoring view; backfills are triggered only via the
    # "Run backfill now" button. Every field is read-only so nothing can be hand-edited.
    readonly_fields = (
        "trigger",
        "scan_from",
        "status",
        "owner",
        "urgent",
        "send_finished_mail",
        "message",
        "queued_job",
        "created_at",
        "started_at",
        "ended_at",
    )

    def has_add_permission(self, request: HttpRequest) -> bool:
        # Jobs are created only via the "Run backfill now" button or the periodic scan,
        # never hand-added through the admin add form.
        return False

    def has_delete_permission(self, request: HttpRequest, obj: object = None) -> bool:
        # Active jobs must be canceled (which revokes their queued work) before deletion;
        # deleting a running job would orphan in-flight LLM calls. Finished jobs follow the
        # normal Django delete-permission check.
        if isinstance(obj, LabelingJob) and obj.status in LabelingJob.ACTIVE_STATUSES:
            return False
        return super().has_delete_permission(request, obj)

    def get_actions(self, request: HttpRequest):
        # Bulk "delete selected" checks delete permission once with no object, bypassing the
        # per-job active-status guard above. Remove it so deletion only happens per-object.
        actions = super().get_actions(request)
        actions.pop("delete_selected", None)
        return actions

    def get_urls(self):
        urls = super().get_urls()
        custom = [
            path(
                "run-backfill/",
                self.admin_site.admin_view(self.run_backfill_view),
                name="labels_labelingjob_run_backfill",
            ),
            path(
                "<int:job_id>/cancel/",
                self.admin_site.admin_view(self.cancel_job_view),
                name="labels_labelingjob_cancel",
            ),
        ]
        return custom + urls

    def cancel_job_view(self, request: HttpRequest, job_id: int) -> HttpResponseRedirect:
        change_url = reverse("admin:labels_labelingjob_change", args=[job_id])
        if request.method != "POST":
            return HttpResponseRedirect(change_url)
        job = LabelingJob.objects.get(pk=job_id)
        if not job.is_cancelable:
            self.message_user(
                request,
                f"Job {job.pk} with status {job.get_status_display()} is not cancelable.",
                level=messages.ERROR,
            )
            return HttpResponseRedirect(change_url)
        cancel_job(job)
        self.message_user(request, f"Canceling job {job.pk}.", level=messages.SUCCESS)
        return HttpResponseRedirect(change_url)

    def run_backfill_view(self, request: HttpRequest) -> HttpResponseRedirect:
        changelist_url = reverse("admin:labels_labelingjob_changelist")
        if request.method != "POST":
            return HttpResponseRedirect(changelist_url)
        try:
            with transaction.atomic():
                job = LabelingJob.objects.create(
                    trigger=LabelingJob.Trigger.MANUAL,
                    status=LabelingJob.Status.PENDING,
                    owner=request.user,
                )
        except IntegrityError:
            self.message_user(request, "A labeling job is already active.", level=messages.ERROR)
            return HttpResponseRedirect(changelist_url)
        job.delay()
        self.message_user(request, f"Started backfill job {job.pk}.", level=messages.SUCCESS)
        return HttpResponseRedirect(changelist_url)


@admin.register(LabelingTask)
class LabelingTaskAdmin(_ReadOnlyAdmin):
    list_display = ("id", "job", "status", "message", "started_at", "ended_at")
    list_filter = ("status",)
    # Inert under read-only; kept for consistency with the other label admins so a future
    # editable admin degrades to an ID input rather than a full LabelingJob dropdown.
    raw_id_fields = ("job",)

    def has_delete_permission(self, request: HttpRequest, obj: object = None) -> bool:
        # Allow deletion so deleting a LabelingJob can cascade to its tasks. Add/change
        # remain blocked via _ReadOnlyAdmin, so tasks are still effectively view-only.
        return True
