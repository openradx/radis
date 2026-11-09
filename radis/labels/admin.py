from dataclasses import dataclass
from typing import Any

from django.contrib import admin, messages
from django.contrib.admin.options import IS_POPUP_VAR
from django.contrib.admin.utils import flatten_fieldsets, unquote
from django.db import IntegrityError, transaction
from django.http import HttpRequest, HttpResponse, HttpResponseRedirect
from django.shortcuts import get_object_or_404
from django.template.response import TemplateResponse
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
    comparable_definition_value,
)

# POST field carrying the fingerprint of the definition the admin confirmed changing.
CONFIRM_DEFINITION_CHANGE = "_confirm_definition_change"


@dataclass(frozen=True)
class DefinitionChangeImpact:
    """What a definition change invalidates: `count` rows (singular `noun`) go stale."""

    count: int
    noun: str
    details: tuple[str, ...] = ()


class _DefinitionConfirmAdmin(admin.ModelAdmin):
    """Asks for confirmation before a change-form save alters the definition (the model's
    `DEFINITION_FIELDS`), and shows how many results or gate answers that makes stale. Saves
    that leave the definition as it is (a no-op save, toggling `active`) go straight through, as
    does the add form, since nothing exists yet to invalidate."""

    confirmation_template = "admin/labels/definition_change_confirmation.html"

    def definition_change_impact(self, obj: Any) -> DefinitionChangeImpact:
        raise NotImplementedError

    def changeform_view(
        self,
        request: HttpRequest,
        object_id: str | None = None,
        form_url: str = "",
        extra_context: dict[str, Any] | None = None,
    ) -> HttpResponse:
        if (
            request.method == "POST"
            and object_id is not None
            and "_saveasnew" not in request.POST  # creates a new object; nothing to invalidate
        ):
            response = self._definition_change_confirmation(request, object_id)
            if response is not None:
                return response
        return super().changeform_view(request, object_id, form_url, extra_context)

    @staticmethod
    def _definition_fingerprint(obj: Any) -> str:
        return obj.definition_updated_at.isoformat()

    def _definition_change_confirmation(
        self, request: HttpRequest, object_id: str
    ) -> HttpResponse | None:
        """The confirmation page, or None to let the regular change view handle the POST:
        upload present, object missing, no permission, already confirmed for the definition as
        it is now, invalid form, definition unchanged, nothing to invalidate."""
        if request.FILES:
            return None  # the page echoes the POST as hidden inputs, which cannot carry files
        obj = self.get_object(request, unquote(object_id))
        if obj is None or not self.has_change_permission(request, obj):
            return None
        # The fingerprint binds a confirmation to the definition the admin saw: a concurrent
        # edit in between changes it, and the page is shown again with the new diff.
        fingerprint = self._definition_fingerprint(obj)
        confirmed = request.POST.get(CONFIRM_DEFINITION_CHANGE)
        if confirmed == fingerprint:
            return None
        definition_fields = self.model.DEFINITION_FIELDS
        # Snapshot before validating: ModelForm.is_valid() writes the cleaned data onto obj.
        old = {f: getattr(obj, f) for f in definition_fields}
        object_display = str(obj)
        fieldsets = self.get_fieldsets(request, obj)
        form_class = self.get_form(request, obj, change=True, fields=flatten_fieldsets(fieldsets))
        form = form_class(request.POST, request.FILES, instance=obj)
        if not form.is_valid():
            return None
        changes = [
            (form.fields[f].label or f, old[f], form.cleaned_data[f])
            for f in definition_fields
            if f in form.cleaned_data
            and comparable_definition_value(form.cleaned_data[f])
            != comparable_definition_value(old[f])
        ]
        if not changes:
            return None
        impact = self.definition_change_impact(obj)
        if impact.count == 0:
            return None
        if confirmed is not None:
            messages.warning(
                request,
                "The definition was changed by someone else since you opened this page. "
                "Review the new differences before confirming.",
            )
        context = {
            **self.admin_site.each_context(request),
            "title": f"Confirm {self.opts.verbose_name} definition change",
            "opts": self.opts,
            "object_display": object_display,
            "object_id": object_id,
            "changes": changes,
            "impact": impact,
            "post_items": [
                (name, values)
                for name, values in request.POST.lists()
                if name not in ("csrfmiddlewaretoken", CONFIRM_DEFINITION_CHANGE)
            ],
            "confirm_flag": CONFIRM_DEFINITION_CHANGE,
            "fingerprint": fingerprint,
            "is_popup": IS_POPUP_VAR in request.POST or IS_POPUP_VAR in request.GET,
            "media": self.media,
        }
        return TemplateResponse(request, self.confirmation_template, context)


@admin.register(LabelGroup)
class LabelGroupAdmin(_DefinitionConfirmAdmin):
    list_display = ("name", "gate_question", "definition_updated_at", "updated_at")
    search_fields = ("name",)  # required for LabelAdmin autocomplete
    ordering = ("name",)
    readonly_fields = ("definition_updated_at", "updated_at")

    def definition_change_impact(self, obj: Any) -> DefinitionChangeImpact:
        gate_answers = GateAnswer.objects.filter(label_group=obj)
        total = gate_answers.count()
        yes = gate_answers.filter(value=GateAnswer.Value.YES).count()
        details = [
            f"{yes} of them currently answer Yes. A Yes that flips to No removes that report's "
            "label results for this group; a No that flips to Yes classifies all of the group's "
            "active labels for that report, one more LLM call each. Results of reports whose "
            "answer does not flip stay fresh."
        ]
        if not Label.objects.filter(group=obj, active=True).exists():
            details.append(
                "This group has no active label, so backfills skip it: its gate answers stay "
                "stale, with no LLM calls, until one of its labels is active again."
            )
        return DefinitionChangeImpact(count=total, noun="gate answer", details=tuple(details))


@admin.register(Label)
class LabelAdmin(_DefinitionConfirmAdmin):
    autocomplete_fields = ["group"]
    list_display = ("name", "group", "active", "definition_updated_at", "updated_at")
    list_filter = ("active", "group")
    search_fields = ("name", "group__name", "description")
    ordering = ("group__name", "name")
    readonly_fields = ("created_at", "definition_updated_at", "updated_at")

    def definition_change_impact(self, obj: Any) -> DefinitionChangeImpact:
        details = ()
        if not obj.active:
            details = (
                "This label is inactive, so backfills skip it: its results stay stale, with no "
                "LLM calls, until it is active again.",
            )
        return DefinitionChangeImpact(
            count=LabelResult.objects.filter(label=obj).count(),
            noun="label result",
            details=details,
        )


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
    # Inert while read-only, but if the guard is ever loosened these degrade to ID inputs
    # instead of a <select> over the huge Report table.
    raw_id_fields = ("report", "label")

    @admin.display(boolean=True, description="Stale")
    def is_stale(self, obj: LabelResult) -> bool:
        return obj.is_stale


@admin.register(GateAnswer)
class GateAnswerAdmin(_ReadOnlyAdmin):
    list_display = ("report", "label_group", "value", "is_stale", "generated_at")
    list_filter = ("value", "label_group")
    search_fields = ("report__document_id", "label_group__name")
    # Inert under read-only; see LabelResultAdmin.
    raw_id_fields = ("report", "label_group")

    @admin.display(boolean=True, description="Stale")
    def is_stale(self, obj: GateAnswer) -> bool:
        return obj.is_stale


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
    # Custom changelist adds a "Run backfill now" button (built-in actions need a selected row).
    change_list_template = "admin/labels/labelingjob/change_list.html"
    # Adds a "Cancel job" button to the read-only detail page when the job is cancelable.
    change_form_template = "admin/labels/labelingjob/change_form.html"
    # get_absolute_url points back at this very admin page (it exists for job links
    # elsewhere), so the "View on site" button would be a circular link — and the
    # sites-framework redirect it goes through breaks when SITE_DOMAIN lacks the port.
    view_on_site = False

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
        # Active jobs must be canceled first (cancel revokes their queued work) — deleting a
        # running job would orphan in-flight LLM calls.
        if isinstance(obj, LabelingJob) and obj.status in LabelingJob.ACTIVE_STATUSES:
            return False
        return super().has_delete_permission(request, obj)

    def get_actions(
        self,
        request: HttpRequest,
        action_location: admin.ActionLocation = admin.ActionLocation.CHANGE_LIST,
    ):
        # Bulk "delete selected" checks delete permission once with no object, bypassing the
        # per-job active-status guard above. Remove it so deletion only happens per-object.
        actions = super().get_actions(request, action_location)
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
        job = get_object_or_404(LabelingJob, pk=job_id)
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
            # delay() inside the transaction so the job row and its queue row commit together —
            # a crash in between would otherwise strand an active-but-never-queued job.
            with transaction.atomic():
                job = LabelingJob.objects.create(
                    trigger=LabelingJob.Trigger.MANUAL,
                    status=LabelingJob.Status.PENDING,
                    owner=request.user,
                )
                job.delay()
        except IntegrityError:
            self.message_user(request, "A labeling job is already active.", level=messages.ERROR)
            return HttpResponseRedirect(changelist_url)
        self.message_user(request, f"Started backfill job {job.pk}.", level=messages.SUCCESS)
        return HttpResponseRedirect(changelist_url)


@admin.register(LabelingTask)
class LabelingTaskAdmin(_ReadOnlyAdmin):
    list_display = ("id", "job", "status", "message", "started_at", "ended_at")
    list_filter = ("status",)
    # Inert under read-only; see LabelResultAdmin.
    raw_id_fields = ("job",)

    def has_delete_permission(self, request: HttpRequest, obj: object = None) -> bool:
        # Deletable only so a non-active job's delete can cascade to its tasks — deleting a
        # live task neither revokes its queued Procrastinate job nor updates job state.
        if isinstance(obj, LabelingTask) and obj.job.status in LabelingJob.ACTIVE_STATUSES:
            return False
        return True

    def get_actions(
        self,
        request: HttpRequest,
        action_location: admin.ActionLocation = admin.ActionLocation.CHANGE_LIST,
    ):
        # No bulk task deletion — the bulk action checks delete permission once with no object,
        # bypassing the active-job guard above.
        actions = super().get_actions(request, action_location)
        actions.pop("delete_selected", None)
        return actions
