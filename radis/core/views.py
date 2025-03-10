from typing import Any, cast

from adit_radis_shared.common.mixins import PageSizeSelectMixin, RelatedFilterMixin
from adit_radis_shared.common.site import THEME_PREFERENCE_KEY
from adit_radis_shared.common.types import AuthenticatedHttpRequest
from adit_radis_shared.common.views import (
    BaseHomeView,
    BaseUpdatePreferencesView,
)
from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.contrib.auth.mixins import (
    LoginRequiredMixin,
    PermissionRequiredMixin,
    UserPassesTestMixin,
)
from django.core.exceptions import SuspiciousOperation
from django.db import transaction
from django.db.models import QuerySet
from django.forms import ModelForm
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.views.generic import CreateView, DeleteView, DetailView, View
from django.views.generic.detail import SingleObjectMixin
from django_filters.filterset import FilterSet
from django_filters.views import FilterView
from django_tables2 import SingleTableMixin, Table
from procrastinate.contrib.django import app

from radis.core.utils.model_utils import reset_tasks

from .models import AnalysisJob, AnalysisTask


@staff_member_required
def admin_section(request: HttpRequest) -> HttpResponse:
    return render(request, "core/admin_section.html")


class HomeView(BaseHomeView):
    template_name = "core/home.html"


class UpdatePreferencesView(BaseUpdatePreferencesView):
    allowed_keys = [THEME_PREFERENCE_KEY]


class AnalysisJobListView(LoginRequiredMixin, SingleTableMixin, PageSizeSelectMixin, FilterView):
    model: type[AnalysisJob]
    table_class: type[Table]
    filterset_class: type[FilterSet]
    template_name: str
    request: AuthenticatedHttpRequest

    def get_queryset(self) -> QuerySet:
        if self.request.user.is_staff and self.request.GET.get("all"):
            return self.model.objects.all()

        return self.model.objects.filter(owner=self.request.user)

    def get_table_kwargs(self) -> dict[str, Any]:
        kwargs = super().get_table_kwargs()

        if not (self.request.user.is_staff and self.request.GET.get("all")):
            kwargs["exclude"] = ("owner",)

        return kwargs


class AnalysisJobCreateView(LoginRequiredMixin, PermissionRequiredMixin, CreateView):
    model: type[AnalysisJob]
    form_class: type[ModelForm]
    template_name: str
    permission_required: str
    request: AuthenticatedHttpRequest
    object: AnalysisJob

    def get_form_kwargs(self) -> dict[str, Any]:
        kwargs = super().get_form_kwargs()
        kwargs["user"] = self.request.user
        return kwargs

    def form_valid(self, form: ModelForm, start_unverified: bool) -> HttpResponse:
        user = self.request.user
        form.instance.owner = user
        response = super().form_valid(form)

        if user.is_staff or start_unverified:
            job = self.object  # set by super().form_valid(form)
            job.status = AnalysisJob.Status.PENDING
            job.save()

            transaction.on_commit(lambda: job.delay())

        return response


class AnalysisJobDetailView(
    LoginRequiredMixin, SingleTableMixin, RelatedFilterMixin, PageSizeSelectMixin, DetailView
):
    table_class: type[Table]
    filterset_class: type[FilterSet]
    model: type[AnalysisJob]
    context_object_name: str
    template_name: str
    request: AuthenticatedHttpRequest

    def get_queryset(self):
        if self.request.user.is_staff:
            return self.model.objects.all()
        return self.model.objects.filter(owner=self.request.user)

    def get_filter_queryset(self) -> QuerySet:
        job = cast(AnalysisJob, self.get_object())
        return job.tasks


class AnalysisJobDeleteView(LoginRequiredMixin, DeleteView):
    model: type[AnalysisJob]
    success_url: str
    success_message = "Job with ID %(id)d was deleted"
    request: AuthenticatedHttpRequest
    object: AnalysisJob

    def get_queryset(self):
        if self.request.user.is_staff:
            return self.model.objects.all()
        return self.model.objects.filter(owner=self.request.user)

    def form_valid(self, form: ModelForm) -> HttpResponse:
        job = cast(AnalysisJob, self.get_object())

        if not job.is_deletable:
            raise SuspiciousOperation(
                f"Job with ID {job.pk} and status {job.get_status_display()} is not deletable."
            )

        # We have to create the success message before we delete the job
        # as the ID afterwards will be None
        success_message = self.success_message % job.__dict__

        job.delete()

        messages.success(self.request, success_message)
        return redirect(self.get_success_url())


class AnalysisJobVerifyView(LoginRequiredMixin, UserPassesTestMixin, SingleObjectMixin, View):
    model: type[AnalysisJob]
    success_message = "Job with ID %(id)d was verified"
    request: AuthenticatedHttpRequest

    def test_func(self) -> bool:
        return self.request.user.is_staff

    def get_queryset(self):
        if self.request.user.is_staff:
            return self.model.objects.all()
        return self.model.objects.filter(owner=self.request.user)

    def post(self, request: AuthenticatedHttpRequest, *args, **kwargs) -> HttpResponse:
        job = cast(AnalysisJob, self.get_object())
        if job.is_verified:
            raise SuspiciousOperation(
                f"Job with ID {job.pk} and status {job.get_status_display()} was already verified."
            )

        job.status = AnalysisJob.Status.PENDING
        job.save()

        transaction.on_commit(lambda: job.delay())

        messages.success(request, self.success_message % job.__dict__)
        return redirect(job)


class AnalysisJobCancelView(LoginRequiredMixin, SingleObjectMixin, View):
    model: type[AnalysisJob]
    success_message = "Job with ID %(id)d was canceled"
    request: AuthenticatedHttpRequest

    def get_queryset(self):
        if self.request.user.is_staff:
            return self.model.objects.all()
        return self.model.objects.filter(owner=self.request.user)

    def post(self, request: AuthenticatedHttpRequest, *args, **kwargs) -> HttpResponse:
        job = cast(AnalysisJob, self.get_object())
        if not job.is_cancelable:
            raise SuspiciousOperation(
                f"Job with ID {job.pk} and status {job.get_status_display()} is not cancelable."
            )

        tasks = job.tasks.filter(status=AnalysisTask.Status.PENDING)
        for task in tasks.only("queued_job_id"):
            queued_job_id = task.queued_job_id
            if queued_job_id is not None:
                app.job_manager.cancel_job_by_id(queued_job_id, delete_job=True)
        tasks.update(status=AnalysisTask.Status.CANCELED)

        if job.tasks.filter(status=AnalysisTask.Status.IN_PROGRESS).exists():
            job.status = AnalysisJob.Status.CANCELING
        else:
            job.status = AnalysisJob.Status.CANCELED
        job.save()

        messages.success(request, self.success_message % job.__dict__)
        return redirect(job)


class AnalysisJobResumeView(LoginRequiredMixin, SingleObjectMixin, View):
    model: type[AnalysisJob]
    success_message = "Job with ID %(id)d will be resumed"
    request: AuthenticatedHttpRequest

    def get_queryset(self):
        if self.request.user.is_staff:
            return self.model.objects.all()
        return self.model.objects.filter(owner=self.request.user)

    def post(self, request: AuthenticatedHttpRequest, *args, **kwargs) -> HttpResponse:
        job = cast(AnalysisJob, self.get_object())
        if not job.is_resumable:
            raise SuspiciousOperation(
                f"Job with ID {job.pk} and status {job.get_status_display()} is not resumable."
            )

        job.tasks.filter(status=AnalysisTask.Status.CANCELED).update(
            status=AnalysisTask.Status.PENDING
        )

        job.status = AnalysisJob.Status.PENDING
        job.save()

        transaction.on_commit(lambda: job.delay())

        messages.success(request, self.success_message % job.__dict__)
        return redirect(job)


class AnalysisJobRetryView(LoginRequiredMixin, SingleObjectMixin, View):
    model: type[AnalysisJob]
    success_message = "Job with ID %(id)d will be retried"
    request: AuthenticatedHttpRequest

    def get_queryset(self):
        if self.request.user.is_staff:
            return self.model.objects.all()
        return self.model.objects.filter(owner=self.request.user)

    def post(self, request: AuthenticatedHttpRequest, *args, **kwargs) -> HttpResponse:
        job = cast(AnalysisJob, self.get_object())
        if not job.is_retriable:
            raise SuspiciousOperation(
                f"Job with ID {job.pk} and status {job.get_status_display()} is not retriable."
            )

        tasks = job.reset_tasks(only_failed=True)

        job.status = AnalysisJob.Status.PENDING
        job.message = ""
        job.save()

        transaction.on_commit(lambda: [task.delay() for task in tasks])

        messages.success(request, self.success_message % job.__dict__)
        return redirect(job)


class AnalysisJobRestartView(LoginRequiredMixin, UserPassesTestMixin, SingleObjectMixin, View):
    model: type[AnalysisJob]
    success_message = "Job with ID %(id)d will be restarted"
    request: AuthenticatedHttpRequest

    def test_func(self) -> bool:
        return self.request.user.is_staff

    def get_queryset(self):
        if self.request.user.is_staff:
            return self.model.objects.all()
        return self.model.objects.filter(owner=self.request.user)

    def post(self, request: AuthenticatedHttpRequest, *args, **kwargs) -> HttpResponse:
        job = cast(AnalysisJob, self.get_object())
        if not request.user.is_staff or not job.is_restartable:
            raise SuspiciousOperation(
                f"Job with ID {job.pk} and status {job.get_status_display()} is not restartable."
            )

        job.tasks.all().delete()

        job.status = AnalysisJob.Status.PENDING
        job.message = ""
        job.save()

        transaction.on_commit(lambda: job.delay())

        messages.success(request, self.success_message % job.__dict__)
        return redirect(job)


class AnalysisTaskDetailView(LoginRequiredMixin, DetailView):
    model: type[AnalysisTask]
    job_url_name: str
    template_name: str
    context_object_name = "task"
    request: AuthenticatedHttpRequest

    def get_queryset(self):
        if self.request.user.is_staff:
            return self.model.objects.all()
        return self.model.objects.filter(job__owner=self.request.user)

    def get_context_data(self, **kwargs) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        context["job_url_name"] = self.job_url_name
        return context


class AnalysisTaskDeleteView(LoginRequiredMixin, DeleteView):
    model: type[AnalysisTask]
    success_message = "Task with ID %(id)d was deleted"
    request: AuthenticatedHttpRequest

    def get_queryset(self):
        if self.request.user.is_staff:
            return self.model.objects.all()
        return self.model.objects.filter(owner=self.request.user)

    def form_valid(self, form: ModelForm) -> HttpResponse:
        task = cast(AnalysisTask, self.get_object())

        if not task.is_deletable:
            raise SuspiciousOperation(
                f"Task with ID {task.pk} and status {task.get_status_display()} is not deletable."
            )

        # We have to create the success message before we delete the task
        # as the ID afterwards will be None
        success_message = self.success_message % task.__dict__

        task.delete()

        task.job.update_job_state()

        messages.success(self.request, success_message)
        return redirect(task.job)


class AnalysisTaskResetView(LoginRequiredMixin, SingleObjectMixin, View):
    model: type[AnalysisTask]
    success_message = "Task with ID %(id)d was reset"
    request: AuthenticatedHttpRequest

    def get_queryset(self):
        if self.request.user.is_staff:
            return self.model.objects.all()
        return self.model.objects.filter(owner=self.request.user)

    def post(self, request: AuthenticatedHttpRequest, *args, **kwargs) -> HttpResponse:
        task = cast(AnalysisTask, self.get_object())
        if not task.is_resettable:
            raise SuspiciousOperation(
                f"Task with ID {task.pk} and status {task.get_status_display()} is not resettable."
            )

        reset_tasks(self.model.objects.filter(pk=task.pk))

        task.job.update_job_state()

        transaction.on_commit(lambda: task.delay())

        messages.success(request, self.success_message % task.__dict__)
        return redirect(task)
