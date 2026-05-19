from typing import Any

from adit_radis_shared.common.types import AuthenticatedHttpRequest
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from django.core.exceptions import SuspiciousOperation
from django.db.models import Prefetch, QuerySet
from django.http import Http404, HttpResponse, HttpResponseRedirect
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
from .tasks import dispatch_backfill_for_set
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
        # The "Eval" button on the detail page renders only when the
        # developer harness is enabled (see LABELS_EVAL_ENABLED). When
        # disabled, the URL reverse would fail because the route is not
        # registered, so the template must skip the button entirely.
        context["labels_eval_enabled"] = settings.LABELS_EVAL_ENABLED
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


class _EvalEnabledMixin:
    """Defense-in-depth gate on the eval views.

    The URL conf in urls.py already only registers the eval routes when
    ``settings.LABELS_EVAL_ENABLED`` is True, so in production the route
    table doesn't even contain these views. This mixin is the second
    layer: if the routes ever do get registered by mistake (a future
    refactor moves them out from under the conditional, an experiment
    branch leaves them on, a misconfigured environment), the view still
    raises ``Http404`` and no data leaks.

    We read the setting at dispatch time (not class-definition time) so
    tests can toggle it with ``override_settings``.
    """

    def dispatch(self, request, *args, **kwargs):
        if not getattr(settings, "LABELS_EVAL_ENABLED", False):
            raise Http404("Evaluation views are disabled in this environment.")
        return super().dispatch(request, *args, **kwargs)  # type: ignore[misc]


class QuestionSetEvalView(_EvalEnabledMixin, LoginRequiredMixin, DetailView):
    """Developer-only: inline DIRECT vs REASONED comparison for the most
    recent EvalSample belonging to this set. If no sample exists, prompts
    to run the seed command.

    Gated on ``settings.LABELS_EVAL_ENABLED`` at two layers — the URL
    conf only registers this route when the flag is True, and the
    ``_EvalEnabledMixin`` re-checks the flag at dispatch as defense in
    depth. In production both gates close; the view is unreachable.
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


class EvalSampleDetailView(_EvalEnabledMixin, LoginRequiredMixin, DetailView):
    """Developer-only: render the evaluation report for a specific
    EvalSample by primary key.

    Same two-layer gate as ``QuestionSetEvalView`` — URL conf inclusion
    plus dispatch-time ``Http404`` if ``LABELS_EVAL_ENABLED`` is False.
    """

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
    """Re-launch a backfill for a question set whose prior job ended in a
    terminal state.

    "Retry" is functionally identical to a fresh launch — the coordinator
    iterates ``missing_reports()`` either way, which naturally skips any
    reports that were already labelled. The button is offered as a separate
    affordance only because it's the natural place for the staff member to
    click after seeing a FAILURE / CANCELED / SUCCESS card.

    The actual dispatch goes through :func:`dispatch_backfill_for_set`, which
    creates a *new* ``BackfillJob`` row under the per-set lock. The previous
    terminal row stays in the database as audit history — you can see all
    retries on the set by querying ``BackfillJob`` ordered by ``created_at``.

    Race fix vs HIGH #2 of the 2026-05-19 review: the older version did an
    ``active_exists`` check **outside** any lock, then in-place reset the
    row to PENDING. The window between those two operations let the nightly
    launcher slip in and create a second active backfill — both would then
    dispatch the same batches and produce duplicate LabelingRun / Answer
    rows. The helper's ``select_for_update`` closes that window: if the
    launcher wins the race, the helper returns ``None`` and we surface the
    "already running" message rather than queueing a duplicate.
    """

    def post(self, request: AuthenticatedHttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        backfill_job = get_object_or_404(BackfillJob, pk=kwargs["pk"])

        # Pre-flight check on the *original* job state. The helper itself
        # doesn't care which job the user is retrying — it just creates a
        # new row if no active backfill exists. We do this check here so a
        # user clicking Retry on a still-running job gets a clear 400 error
        # rather than a silent "we made you a new job, here it is."
        if not backfill_job.is_retryable:
            raise SuspiciousOperation(
                f"Backfill job {backfill_job.pk} with status "
                f"{backfill_job.get_status_display()} cannot be retried."
            )

        new_job = dispatch_backfill_for_set(backfill_job.question_set)
        if new_job is None:
            messages.info(
                request,
                f"Another backfill for {backfill_job.question_set.name} is already in "
                "progress; it will pick up any missing labels.",
            )
            return redirect("question_set_detail", pk=backfill_job.question_set_id)

        messages.success(
            request,
            f"Retrying backfill for {backfill_job.question_set.name}.",
        )
        return redirect("question_set_detail", pk=backfill_job.question_set_id)


class BackfillLaunchView(StaffRequiredMixin, View):
    """Manually kick off a backfill for a question set, bypassing the nightly cron.

    The nightly launcher is the safe default — it batches work off-peak and
    debounces bursty staff edits into a single nightly pass. This view is
    the explicit override for the case where a self-aware staff member has
    finished editing and doesn't want to wait until 21:00 to see results.

    Three guards before the helper sees the request:

    * The set must be active. Inactive sets aren't labelled at all.
    * There must be outstanding work (``missing_reports().exists()``).
      Otherwise the helper would happily create a no-op coordinator that
      flips straight to SUCCESS — harmless but wastes a row.
    * No active backfill must exist. The helper enforces this under a
      lock; we surface a friendly message rather than creating a duplicate.

    The button rendering its trigger is hidden in the template while the
    set is locked, so under normal use this view never sees a 409-like
    situation. The guards are defensive.
    """

    def post(self, request: AuthenticatedHttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        question_set = get_object_or_404(QuestionSet, pk=kwargs["pk"])

        if not question_set.is_active:
            messages.warning(
                request,
                f"Question set '{question_set.name}' is inactive. Activate it before launching a backfill.",
            )
            return redirect("question_set_detail", pk=question_set.id)

        if not question_set.missing_reports().exists():
            messages.info(
                request,
                f"No outstanding labelling work for '{question_set.name}'. Nothing to do.",
            )
            return redirect("question_set_detail", pk=question_set.id)

        backfill_job = dispatch_backfill_for_set(question_set)
        if backfill_job is None:
            messages.info(
                request,
                f"A backfill for '{question_set.name}' is already running.",
            )
            return redirect("question_set_detail", pk=question_set.id)

        messages.success(
            request,
            f"Backfill started for '{question_set.name}'.",
        )
        return redirect("question_set_detail", pk=question_set.id)
