from typing import Any

from adit_radis_shared.common.types import AuthenticatedHttpRequest
from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from django.core.exceptions import SuspiciousOperation
from django.db import transaction
from django.db.models import Prefetch, QuerySet
from django.http import HttpResponse, HttpResponseRedirect
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse, reverse_lazy
from django.utils import timezone
from django.views import View
from django.views.generic import CreateView, DeleteView, DetailView, UpdateView
from django_tables2 import SingleTableView

from .forms import LabelGroupForm, LabelQuestionForm
from .models import LabelBackfillJob, LabelGroup, LabelQuestion
from .tables import LabelGroupTable
from .tasks import enqueue_label_group_backfill


class StaffRequiredMixin(LoginRequiredMixin, UserPassesTestMixin):
    request: AuthenticatedHttpRequest

    def test_func(self) -> bool:
        return self.request.user.is_staff


class LabelGroupListView(LoginRequiredMixin, SingleTableView):
    model = LabelGroup
    table_class = LabelGroupTable
    template_name = "labels/label_group_list.html"
    paginate_by = 30
    request: AuthenticatedHttpRequest

    def get_queryset(self) -> QuerySet[LabelGroup]:
        return LabelGroup.objects.all().order_by("order", "name")


class LabelGroupDetailView(LoginRequiredMixin, DetailView):
    model = LabelGroup
    template_name = "labels/label_group_detail.html"

    def get_queryset(self) -> QuerySet[LabelGroup]:
        return LabelGroup.objects.prefetch_related(
            Prefetch(
                "questions",
                queryset=LabelQuestion.objects.prefetch_related("choices").order_by(
                    "order", "label"
                ),
            )
        )

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        context["backfill_job"] = (
            LabelBackfillJob.objects.filter(label_group=self.object).order_by("-created_at").first()
        )
        return context


class LabelGroupCreateView(StaffRequiredMixin, CreateView):
    template_name = "labels/label_group_form.html"
    form_class = LabelGroupForm
    success_url = reverse_lazy("label_group_list")


class LabelGroupUpdateView(StaffRequiredMixin, UpdateView):
    template_name = "labels/label_group_form.html"
    form_class = LabelGroupForm
    model = LabelGroup

    def get_success_url(self) -> str:
        return reverse("label_group_detail", kwargs={"pk": self.object.pk})


class LabelGroupDeleteView(StaffRequiredMixin, DeleteView):
    model = LabelGroup
    success_url = reverse_lazy("label_group_list")
    template_name = "labels/label_group_confirm_delete.html"


class LabelQuestionCreateView(StaffRequiredMixin, CreateView):
    template_name = "labels/label_question_form.html"
    form_class = LabelQuestionForm
    model = LabelQuestion
    request: AuthenticatedHttpRequest

    def dispatch(self, request, *args, **kwargs):
        self.group = LabelGroup.objects.get(pk=kwargs["group_pk"])
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        ctx = super().get_context_data(**kwargs)
        ctx["group"] = self.group
        return ctx

    def get_form_kwargs(self) -> dict[str, Any]:
        kwargs = super().get_form_kwargs()
        kwargs["group"] = self.group
        return kwargs

    def form_valid(self, form) -> HttpResponse:
        form.instance.group = self.group
        self.object = form.save()
        return HttpResponseRedirect(self.get_success_url())

    def get_success_url(self) -> str:
        return reverse("label_group_detail", kwargs={"pk": self.group.pk})


class LabelQuestionUpdateView(StaffRequiredMixin, UpdateView):
    template_name = "labels/label_question_form.html"
    form_class = LabelQuestionForm
    model = LabelQuestion
    request: AuthenticatedHttpRequest

    def dispatch(self, request, *args, **kwargs):
        self.group = LabelGroup.objects.get(pk=kwargs["group_pk"])
        return super().dispatch(request, *args, **kwargs)

    def get_queryset(self) -> QuerySet[LabelQuestion]:
        return LabelQuestion.objects.filter(group=self.group).prefetch_related("choices")

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        ctx = super().get_context_data(**kwargs)
        ctx["group"] = self.group
        return ctx

    def get_form_kwargs(self) -> dict[str, Any]:
        kwargs = super().get_form_kwargs()
        kwargs["group"] = self.group
        return kwargs

    def form_valid(self, form) -> HttpResponse:
        self.object = form.save()
        return HttpResponseRedirect(self.get_success_url())

    def get_success_url(self) -> str:
        return reverse("label_group_detail", kwargs={"pk": self.group.pk})


class LabelQuestionDeleteView(StaffRequiredMixin, DeleteView):
    model = LabelQuestion
    template_name = "labels/label_question_confirm_delete.html"

    def dispatch(self, request, *args, **kwargs):
        self.group = LabelGroup.objects.get(pk=kwargs["group_pk"])
        return super().dispatch(request, *args, **kwargs)

    def get_queryset(self) -> QuerySet[LabelQuestion]:
        return LabelQuestion.objects.filter(group=self.group)

    def get_success_url(self) -> str:
        return reverse("label_group_detail", kwargs={"pk": self.group.pk})


class LabelBackfillCancelView(StaffRequiredMixin, View):
    def post(self, request: AuthenticatedHttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        backfill_job = get_object_or_404(LabelBackfillJob, pk=kwargs["pk"])

        if not backfill_job.is_cancelable:
            raise SuspiciousOperation(
                f"Backfill job {backfill_job.pk} with status "
                f"{backfill_job.get_status_display()} is not cancelable."
            )

        # Snapshot how much work was done before the cancel so the progress
        # display freezes at that point instead of showing 0%.
        remaining = backfill_job.label_group.missing_reports().count()
        processed_at_cancel = max(backfill_job.total_reports - remaining, 0)

        # Conditional UPDATE: if a worker has already finalized the job, the
        # cancel quietly no-ops rather than fighting the terminal state.
        LabelBackfillJob.objects.filter(
            pk=backfill_job.pk,
            status__in=[
                LabelBackfillJob.Status.PENDING,
                LabelBackfillJob.Status.IN_PROGRESS,
            ],
        ).update(
            status=LabelBackfillJob.Status.CANCELED,
            ended_at=timezone.now(),
            processed_reports=processed_at_cancel,
        )

        messages.success(
            request,
            f"Backfill for {backfill_job.label_group.name} canceled.",
        )
        return redirect("label_group_detail", pk=backfill_job.label_group_id)


class LabelBackfillRetryView(StaffRequiredMixin, View):
    def post(self, request: AuthenticatedHttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        backfill_job = get_object_or_404(LabelBackfillJob, pk=kwargs["pk"])

        if not backfill_job.is_retryable:
            raise SuspiciousOperation(
                f"Backfill job {backfill_job.pk} with status "
                f"{backfill_job.get_status_display()} cannot be retried."
            )

        # If another backfill for the same group is already active, retrying
        # this one would just duplicate work — the active backfill's coordinator
        # already picks up missing reports.
        active_exists = (
            LabelBackfillJob.objects.filter(
                label_group_id=backfill_job.label_group_id,
                status__in=[
                    LabelBackfillJob.Status.PENDING,
                    LabelBackfillJob.Status.IN_PROGRESS,
                ],
            )
            .exclude(pk=backfill_job.pk)
            .exists()
        )
        if active_exists:
            messages.info(
                request,
                f"Another backfill for {backfill_job.label_group.name} is already in "
                "progress; it will pick up any missing labels.",
            )
            return redirect("label_group_detail", pk=backfill_job.label_group_id)

        backfill_job.status = LabelBackfillJob.Status.PENDING
        backfill_job.message = ""
        backfill_job.started_at = None
        backfill_job.ended_at = None
        backfill_job.save()

        transaction.on_commit(
            lambda: enqueue_label_group_backfill.defer(
                label_group_id=backfill_job.label_group_id,
                backfill_job_id=backfill_job.id,
            )
        )

        messages.success(
            request,
            f"Retrying backfill for {backfill_job.label_group.name}.",
        )
        return redirect("label_group_detail", pk=backfill_job.label_group_id)
