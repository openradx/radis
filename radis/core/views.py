from typing import Any, cast

from django.conf import settings
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
from django.forms import Form, ModelForm
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.urls import re_path, reverse_lazy
from django.views.generic import CreateView, DeleteView, DetailView, View
from django.views.generic.base import TemplateView
from django.views.generic.detail import SingleObjectMixin
from django.views.generic.edit import FormView
from django_filters.filterset import FilterSet
from django_filters.views import FilterView
from django_tables2 import SingleTableMixin, Table

from adit_radis_shared.common.forms import BroadcastForm
from adit_radis_shared.common.mixins import PageSizeSelectMixin, RelatedFilterMixin
from adit_radis_shared.common.types import AuthenticatedHttpRequest
from adit_radis_shared.common.views import AdminProxyView, BaseUpdatePreferencesView
from radis.celery import app as celery_app
from radis.core.utils.model_utils import reset_tasks

from .models import AnalysisJob, AnalysisTask, CoreSettings
from .tasks import broadcast_mail

THEME = "theme"


@staff_member_required
def admin_section(request: HttpRequest) -> HttpResponse:
    return render(request, "core/admin_section.html")


class BroadcastView(LoginRequiredMixin, UserPassesTestMixin, FormView):
    template_name = "core/broadcast.html"
    form_class = BroadcastForm
    success_url = reverse_lazy("broadcast")
    request: AuthenticatedHttpRequest

    def test_func(self) -> bool:
        return self.request.user.is_staff

    def form_valid(self, form: Form) -> HttpResponse:
        subject = form.cleaned_data["subject"]
        message = form.cleaned_data["message"]

        broadcast_mail.delay(subject, message)

        messages.add_message(
            self.request,
            messages.SUCCESS,
            "Mail queued for sending successfully",
        )

        return super().form_valid(form)


class HomeView(TemplateView):
    template_name = "core/home.html"

    def get_context_data(self, **kwargs) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        core_settings = CoreSettings.get()
        assert core_settings
        context["announcement"] = core_settings.announcement
        return context


class UpdatePreferencesView(BaseUpdatePreferencesView):
    allowed_keys = [THEME]


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

    def form_valid(self, form: ModelForm, transfer_unverified: bool) -> HttpResponse:
        user = self.request.user
        form.instance.owner = user
        response = super().form_valid(form)

        job = self.object  # set by super().form_valid(form)
        if user.is_staff or transfer_unverified:
            job.status = AnalysisJob.Status.PREPARING
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
                f"Job with ID {job.id} and status {job.get_status_display()} is not deletable."
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
                f"Job with ID {job.id} and status {job.get_status_display()} was already verified."
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
                f"Job with ID {job.id} and status {job.get_status_display()} is not cancelable."
            )

        tasks = job.tasks.filter(status=AnalysisTask.Status.PENDING)
        tasks.update(status=AnalysisTask.Status.CANCELED)
        for task in tasks.only("celery_task_id"):
            if task.celery_task_id:
                # Cave, can only revoke tasks that are not already fetched by a worker.
                # So the worker will check again each task if it was cancelled.
                celery_app.control.revoke(task.celery_task_id)

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
                f"Job with ID {job.id} and status {job.get_status_display()} is not resumable."
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
                f"Job with ID {job.id} and status {job.get_status_display()} is not retriable."
            )

        job.reset_tasks(only_failed=True)

        job.status = AnalysisJob.Status.PENDING
        job.save()

        transaction.on_commit(lambda: job.delay())

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
                f"Job with ID {job.id} and status {job.get_status_display()} is not restartable."
            )

        job.tasks.all().delete()

        job.status = AnalysisJob.Status.PREPARING
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
                f"Task with ID {task.id} and status {task.get_status_display()} is not deletable."
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
                f"Task with ID {task.id} and status {task.get_status_display()} is not resettable."
            )

        reset_tasks(self.model.objects.filter(id=task.id))

        task.job.update_job_state()

        transaction.on_commit(lambda: task.delay())

        messages.success(request, self.success_message % task.__dict__)
        return redirect(task)


class FlowerProxyView(AdminProxyView):
    upstream = f"http://{settings.FLOWER_HOST}:{settings.FLOWER_PORT}"  # type: ignore
    url_prefix = "flower"
    rewrite = ((rf"^/{url_prefix}$", rf"/{url_prefix}/"),)

    @classmethod
    def as_url(cls):
        # Flower needs a bit different setup then the other proxy views as flower
        # uses a prefix itself (see docker compose service)
        return re_path(rf"^(?P<path>{cls.url_prefix}.*)$", cls.as_view())


class RabbitManagementProxyView(AdminProxyView):
    upstream = (
        f"http://{settings.RABBIT_MANAGEMENT_HOST}:" f"{settings.RABBIT_MANAGEMENT_PORT}"  # type: ignore
    )
    url_prefix = "rabbit"
    rewrite = ((rf"^/{url_prefix}$", r"/"),)
