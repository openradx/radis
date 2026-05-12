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

from .forms import QuestionForm, QuestionSetForm
from .models import (
    AnswerOption,
    BackfillJob,
    EvalSample,
    Question,
    QuestionSet,
    is_question_set_locked,
)
from .tables import QuestionSetTable
from .tasks import enqueue_question_set_backfill
from .utils.eval_metrics import compute_eval


class StaffRequiredMixin(LoginRequiredMixin, UserPassesTestMixin):
    request: AuthenticatedHttpRequest

    def test_func(self) -> bool:
        return self.request.user.is_staff


class _SetLockGuardMixin:
    """Reject edits to a question set (or any of its questions/options) while a
    backfill is in progress for that set. The lock is purely DB-derived from
    BackfillJob.status so no separate flag can drift.
    """

    locked_question_set_id: int | None = None

    def _guard_locked(self, request, question_set_id: int) -> HttpResponse | None:
        if is_question_set_locked(question_set_id):
            messages.warning(
                request,
                "A backfill is running for this question set. Edits are locked "
                "until it finishes.",
            )
            return redirect("question_set_detail", pk=question_set_id)
        return None


class QuestionSetListView(LoginRequiredMixin, SingleTableView):
    model = QuestionSet
    table_class = QuestionSetTable
    template_name = "labels/question_set_list.html"
    paginate_by = 30
    request: AuthenticatedHttpRequest

    def get_queryset(self) -> QuerySet[QuestionSet]:
        return QuestionSet.objects.all().order_by("order", "name")


class QuestionSetDetailView(LoginRequiredMixin, DetailView):
    model = QuestionSet
    template_name = "labels/question_set_detail.html"

    def get_queryset(self) -> QuerySet[QuestionSet]:
        return QuestionSet.objects.prefetch_related(
            Prefetch(
                "questions",
                queryset=Question.objects.prefetch_related(
                    Prefetch("options", queryset=AnswerOption.objects.order_by("order", "label"))
                ).order_by("order", "label"),
            )
        )

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        context["backfill_job"] = (
            BackfillJob.objects.filter(question_set=self.object)
            .order_by("-created_at")
            .first()
        )
        context["is_locked"] = self.object.is_locked
        return context


class QuestionSetCreateView(StaffRequiredMixin, CreateView):
    template_name = "labels/question_set_form.html"
    form_class = QuestionSetForm
    success_url = reverse_lazy("question_set_list")


class QuestionSetUpdateView(StaffRequiredMixin, _SetLockGuardMixin, UpdateView):
    template_name = "labels/question_set_form.html"
    form_class = QuestionSetForm
    model = QuestionSet

    def dispatch(self, request, *args, **kwargs):
        guard = self._guard_locked(request, kwargs["pk"])
        if guard is not None:
            return guard
        return super().dispatch(request, *args, **kwargs)

    def get_success_url(self) -> str:
        return reverse("question_set_detail", kwargs={"pk": self.object.pk})


class QuestionSetDeleteView(StaffRequiredMixin, _SetLockGuardMixin, DeleteView):
    model = QuestionSet
    success_url = reverse_lazy("question_set_list")
    template_name = "labels/question_set_confirm_delete.html"

    def dispatch(self, request, *args, **kwargs):
        guard = self._guard_locked(request, kwargs["pk"])
        if guard is not None:
            return guard
        return super().dispatch(request, *args, **kwargs)


class QuestionCreateView(StaffRequiredMixin, _SetLockGuardMixin, CreateView):
    template_name = "labels/question_form.html"
    form_class = QuestionForm
    model = Question
    request: AuthenticatedHttpRequest

    def dispatch(self, request, *args, **kwargs):
        guard = self._guard_locked(request, kwargs["question_set_pk"])
        if guard is not None:
            return guard
        self.question_set = QuestionSet.objects.get(pk=kwargs["question_set_pk"])
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        ctx = super().get_context_data(**kwargs)
        ctx["question_set"] = self.question_set
        return ctx

    def get_form_kwargs(self) -> dict[str, Any]:
        kwargs = super().get_form_kwargs()
        kwargs["question_set"] = self.question_set
        return kwargs

    def form_valid(self, form) -> HttpResponse:
        form.instance.question_set = self.question_set
        self.object = form.save()
        return HttpResponseRedirect(self.get_success_url())

    def get_success_url(self) -> str:
        return reverse("question_set_detail", kwargs={"pk": self.question_set.pk})


class QuestionUpdateView(StaffRequiredMixin, _SetLockGuardMixin, UpdateView):
    template_name = "labels/question_form.html"
    form_class = QuestionForm
    model = Question
    request: AuthenticatedHttpRequest

    def dispatch(self, request, *args, **kwargs):
        guard = self._guard_locked(request, kwargs["question_set_pk"])
        if guard is not None:
            return guard
        self.question_set = QuestionSet.objects.get(pk=kwargs["question_set_pk"])
        return super().dispatch(request, *args, **kwargs)

    def get_queryset(self) -> QuerySet[Question]:
        return Question.objects.filter(question_set=self.question_set).prefetch_related(
            "options"
        )

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        ctx = super().get_context_data(**kwargs)
        ctx["question_set"] = self.question_set
        return ctx

    def get_form_kwargs(self) -> dict[str, Any]:
        kwargs = super().get_form_kwargs()
        kwargs["question_set"] = self.question_set
        return kwargs

    def form_valid(self, form) -> HttpResponse:
        # An edit to label/question text invalidates older answers, so bump
        # the version. Old answers retain their snapshot of the prior version
        # and remain attributable.
        instance = form.save(commit=False)
        original = Question.objects.get(pk=instance.pk)
        if (instance.label != original.label) or (instance.question != original.question):
            instance.version = original.version + 1
        instance.save()
        self.object = instance
        return HttpResponseRedirect(self.get_success_url())

    def get_success_url(self) -> str:
        return reverse("question_set_detail", kwargs={"pk": self.question_set.pk})


class QuestionDeleteView(StaffRequiredMixin, _SetLockGuardMixin, DeleteView):
    model = Question
    template_name = "labels/question_confirm_delete.html"

    def dispatch(self, request, *args, **kwargs):
        guard = self._guard_locked(request, kwargs["question_set_pk"])
        if guard is not None:
            return guard
        self.question_set = QuestionSet.objects.get(pk=kwargs["question_set_pk"])
        return super().dispatch(request, *args, **kwargs)

    def get_queryset(self) -> QuerySet[Question]:
        return Question.objects.filter(question_set=self.question_set)

    def get_success_url(self) -> str:
        return reverse("question_set_detail", kwargs={"pk": self.question_set.pk})


class BackfillCancelView(StaffRequiredMixin, View):
    def post(self, request: AuthenticatedHttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        backfill_job = get_object_or_404(BackfillJob, pk=kwargs["pk"])

        if not backfill_job.is_cancelable:
            raise SuspiciousOperation(
                f"Backfill job {backfill_job.pk} with status "
                f"{backfill_job.get_status_display()} is not cancelable."
            )

        # Snapshot how much work was done before the cancel so the progress
        # display freezes at that point instead of showing 0%.
        remaining = backfill_job.question_set.missing_reports().count()
        processed_at_cancel = max(backfill_job.total_reports - remaining, 0)

        # Conditional UPDATE: if a worker has already finalized the job, the
        # cancel quietly no-ops rather than fighting the terminal state.
        BackfillJob.objects.filter(
            pk=backfill_job.pk,
            status__in=[BackfillJob.Status.PENDING, BackfillJob.Status.IN_PROGRESS],
        ).update(
            status=BackfillJob.Status.CANCELED,
            ended_at=timezone.now(),
            processed_reports=processed_at_cancel,
        )

        messages.success(
            request,
            f"Backfill for {backfill_job.question_set.name} canceled.",
        )
        return redirect("question_set_detail", pk=backfill_job.question_set_id)


class QuestionSetEvalView(LoginRequiredMixin, DetailView):
    """Inline DIRECT vs REASONED comparison for the most recent EvalSample
    belonging to this set. If no sample exists, prompts to run the seed
    command.
    """

    model = QuestionSet
    template_name = "labels/eval_report.html"

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        ctx = super().get_context_data(**kwargs)
        question_set: QuestionSet = self.object  # type: ignore[assignment]
        sample = question_set.eval_samples.order_by("-created_at").first()
        ctx["sample"] = sample
        ctx["question_set_id"] = question_set.id
        ctx["report"] = compute_eval(sample) if sample is not None else None
        return ctx


class EvalSampleDetailView(LoginRequiredMixin, DetailView):
    model = EvalSample
    template_name = "labels/eval_report.html"

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        ctx = super().get_context_data(**kwargs)
        sample: EvalSample = self.object  # type: ignore[assignment]
        ctx["sample"] = sample
        ctx["question_set_id"] = sample.question_set_id
        ctx["report"] = compute_eval(sample)
        return ctx


class BackfillRetryView(StaffRequiredMixin, View):
    def post(self, request: AuthenticatedHttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        backfill_job = get_object_or_404(BackfillJob, pk=kwargs["pk"])

        if not backfill_job.is_retryable:
            raise SuspiciousOperation(
                f"Backfill job {backfill_job.pk} with status "
                f"{backfill_job.get_status_display()} cannot be retried."
            )

        active_exists = (
            BackfillJob.objects.filter(
                question_set_id=backfill_job.question_set_id,
                status__in=[BackfillJob.Status.PENDING, BackfillJob.Status.IN_PROGRESS],
            )
            .exclude(pk=backfill_job.pk)
            .exists()
        )
        if active_exists:
            messages.info(
                request,
                f"Another backfill for {backfill_job.question_set.name} is already in "
                "progress; it will pick up any missing labels.",
            )
            return redirect("question_set_detail", pk=backfill_job.question_set_id)

        backfill_job.status = BackfillJob.Status.PENDING
        backfill_job.message = ""
        backfill_job.started_at = None
        backfill_job.ended_at = None
        backfill_job.save()

        transaction.on_commit(
            lambda: enqueue_question_set_backfill.defer(
                question_set_id=backfill_job.question_set_id,
                backfill_job_id=backfill_job.id,
            )
        )

        messages.success(
            request,
            f"Retrying backfill for {backfill_job.question_set.name}.",
        )
        return redirect("question_set_detail", pk=backfill_job.question_set_id)
