import csv
import io
from typing import Any, Type, cast

from adit_radis_shared.common.mixins import (
    PageSizeSelectMixin,
)
from adit_radis_shared.common.types import AuthenticatedHttpRequest
from django.conf import settings
from django.contrib.auth.mixins import (
    LoginRequiredMixin,
    PermissionRequiredMixin,
    UserPassesTestMixin,
)
from django.core.exceptions import SuspiciousOperation
from django.db import transaction
from django.db.models import QuerySet
from django.forms import BaseInlineFormSet
from django.http import FileResponse, HttpResponse
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse_lazy
from django.views import View
from django.views.generic import DetailView
from django_tables2 import SingleTableMixin, tables
from formtools.wizard.views import SessionWizardView

from radis.core.views import (
    AnalysisJobCancelView,
    AnalysisJobDeleteView,
    AnalysisJobDetailView,
    AnalysisJobListView,
    AnalysisJobRestartView,
    AnalysisJobResumeView,
    AnalysisJobRetryView,
    AnalysisJobVerifyView,
    AnalysisTaskDeleteView,
    AnalysisTaskDetailView,
    AnalysisTaskResetView,
    BaseUpdatePreferencesView,
)
from radis.search.utils.query_parser import QueryParser

from .filters import ExtractionInstanceFilter, ExtractionJobFilter, ExtractionTaskFilter
from .forms import (
    OutputFieldFormSet,
    SearchForm,
    SummaryForm,
)
from .mixins import ExtractionsLockedMixin
from .models import (
    ExtractionInstance,
    ExtractionJob,
    ExtractionResultExport,
    ExtractionTask,
)
from .tables import (
    ExtractionInstanceTable,
    ExtractionJobTable,
    ExtractionResultsTable,
    ExtractionTaskTable,
)
from .utils.csv import sanitize_csv_value

EXTRACTIONS_SEARCH_PROVIDER = "extractions_search_provider"

class ExtractionUpdatePreferencesView(ExtractionsLockedMixin, BaseUpdatePreferencesView):
    allowed_keys = [
        EXTRACTIONS_SEARCH_PROVIDER,
    ]


class ExtractionJobListView(ExtractionsLockedMixin, AnalysisJobListView):
    model = ExtractionJob
    table_class = ExtractionJobTable
    filterset_class = ExtractionJobFilter
    template_name = "extractions/extraction_job_list.html"


class ExtractionJobWizardView(
    LoginRequiredMixin, PermissionRequiredMixin, UserPassesTestMixin, SessionWizardView
):
    SEARCH_STEP = "0"
    OUTPUT_FIELDS_STEP = "1"
    SUMMARY_STEP = "2"

    form_list = [SearchForm, OutputFieldFormSet, SummaryForm]
    permission_required = "extractions.add_extractionjob"
    permission_denied_message = "You must be logged in and have an active group"
    request: AuthenticatedHttpRequest

    def test_func(self) -> bool | None:
        return self.request.user.active_group is not None

    def get_form_kwargs(self, step=None):
        kwargs = super().get_form_kwargs(step)
        if step == ExtractionJobWizardView.SEARCH_STEP:
            kwargs["user"] = self.request.user
        return kwargs

    def get_context_data(self, form, **kwargs):
        context = super().get_context_data(form, **kwargs)

        if self.steps.current == ExtractionJobWizardView.SEARCH_STEP:
            pass

        elif self.steps.current == ExtractionJobWizardView.OUTPUT_FIELDS_STEP:
            context["formset"] = form

            data = self.get_cleaned_data_for_step(ExtractionJobWizardView.SEARCH_STEP)
            assert data and isinstance(data, dict)

            context["fixed_query"] = data.get("fixed_query")
            context["retrieval_count"] = data["retrieval_count"]
            self.storage.extra_data["retrieval_count"] = data["retrieval_count"]

        elif self.steps.current == ExtractionJobWizardView.SUMMARY_STEP:
            search_data = self.get_cleaned_data_for_step(ExtractionJobWizardView.SEARCH_STEP)
            output_fields = self.get_cleaned_data_for_step(
                ExtractionJobWizardView.OUTPUT_FIELDS_STEP
            )

            context["search"] = search_data
            context["output_fields"] = output_fields
            context["retrieval_count"] = self.storage.extra_data["retrieval_count"]

        return context

    def get_template_names(self) -> list[str]:
        step = self.steps.current
        if step == ExtractionJobWizardView.SEARCH_STEP:
            return ["extractions/extraction_job_search_form.html"]
        elif step == ExtractionJobWizardView.OUTPUT_FIELDS_STEP:
            return ["extractions/extraction_job_output_fields_form.html"]
        elif step == ExtractionJobWizardView.SUMMARY_STEP:
            return ["extractions/extraction_job_wizard_summary.html"]
        else:
            raise SuspiciousOperation(f"Invalid wizard step: {step}")

    def done(
        self,
        form_objs: tuple[SearchForm, BaseInlineFormSet, SummaryForm],
        **kwargs,
    ):
        user = self.request.user

        with transaction.atomic():
            summary_form = form_objs[2]
            job_form = form_objs[0]
            if summary_form.cleaned_data["send_finished_mail"]:
                job_form.cleaned_data["send_finished_mail"] = True

            job: ExtractionJob = job_form.save(commit=False)

            query = job.query
            query_node, fixes = QueryParser().parse(query)
            if len(fixes) > 0:
                # The query was already validated that it is not empty by the form
                assert query_node
                job.query = QueryParser.unparse(query_node)

            group = user.active_group
            assert group

            job.group = group
            job.owner = user
            job.save()

            # Save output fields
            form_objs[1].instance = job
            form_objs[1].save()

            if user.is_staff or settings.START_EXTRACTION_JOB_UNVERIFIED:
                job.status = ExtractionJob.Status.PENDING
                job.save()

                transaction.on_commit(lambda: job.delay())

        return redirect(job)


class ExtractionJobDetailView(AnalysisJobDetailView):
    model = ExtractionJob
    table_class = ExtractionTaskTable
    filterset_class = ExtractionTaskFilter
    context_object_name = "job"
    template_name = "extractions/extraction_job_detail.html"


class ExtractionJobDeleteView(ExtractionsLockedMixin, AnalysisJobDeleteView):
    model = ExtractionJob
    success_url = cast(str, reverse_lazy("extraction_job_list"))


class ExtractionJobVerifyView(ExtractionsLockedMixin, AnalysisJobVerifyView):
    model = ExtractionJob


class ExtractionJobCancelView(ExtractionsLockedMixin, AnalysisJobCancelView):
    model = ExtractionJob


class ExtractionJobResumeView(ExtractionsLockedMixin, AnalysisJobResumeView):
    model = ExtractionJob


class ExtractionJobRetryView(ExtractionsLockedMixin, AnalysisJobRetryView):
    model = ExtractionJob


class ExtractionJobRestartView(ExtractionsLockedMixin, AnalysisJobRestartView):
    model = ExtractionJob


class ExtractionTaskDetailView(ExtractionsLockedMixin, AnalysisTaskDetailView, SingleTableMixin):
    model = ExtractionTask
    table_class = ExtractionInstanceTable
    filterset_class = ExtractionInstanceFilter
    job_url_name = "extraction_job_detail"
    template_name = "extractions/extraction_task_detail.html"

    def get_context_data(self, **kwargs) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        task = cast(ExtractionTask, self.get_object())
        self.object_list = task.instances.all()
        table = self.get_table()
        context[self.get_context_table_name(table)] = table
        return context


class ExtractionTaskDeleteView(ExtractionsLockedMixin, AnalysisTaskDeleteView):
    model = ExtractionTask


class ExtractionTaskResetView(ExtractionsLockedMixin, AnalysisTaskResetView):
    model = ExtractionTask


class ExtractionInstanceDetailView(LoginRequiredMixin, DetailView):
    context_object_name = "instance"
    template_name = "extractions/extraction_instance_detail.html"
    request: AuthenticatedHttpRequest

    def get_queryset(self) -> QuerySet[ExtractionInstance]:
        if self.request.user.is_staff:
            return ExtractionInstance.objects.all()
        return ExtractionInstance.objects.filter(task__job__owner=self.request.user)


class ExtractionResultListView(
    ExtractionsLockedMixin,
    LoginRequiredMixin,
    SingleTableMixin,
    PageSizeSelectMixin,
    DetailView,
):
    model = ExtractionJob
    table_class = ExtractionResultsTable
    context_object_name = "job"
    template_name = "extractions/extraction_result_list.html"
    request: AuthenticatedHttpRequest
    object_list: QuerySet[ExtractionInstance]

    def get_queryset(self) -> QuerySet[ExtractionJob]:
        assert self.model
        model = cast(Type[ExtractionJob], self.model)
        if self.request.user.is_staff:
            return model.objects.all()
        return model.objects.filter(owner=self.request.user)

    def get_table(self, **kwargs):
        job = cast(ExtractionJob, self.get_object())

        extra_columns = []
        for field in job.output_fields.all():
            extra_columns.append(
                (field.name, tables.Column(field.name, accessor=f"output.{field.name}"))
            )

        return super().get_table(extra_columns=extra_columns, **kwargs)

    def get_table_data(self):
        job = cast(ExtractionJob, self.get_object())
        return ExtractionInstance.objects.filter(task__job=job)

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        job = cast(ExtractionJob, self.get_object())
        context["download_available"] = (
            job.status in ExtractionResultDownloadView.FINISHED_STATUSES
        )
        export = job.result_exports.first()
        context["latest_export"] = export
        context["export_ready"] = (
            export is not None
            and export.status == ExtractionResultExport.Status.COMPLETED
            and bool(export.file)
        )
        context["export_in_progress"] = export is not None and export.status in {
            ExtractionResultExport.Status.PENDING,
            ExtractionResultExport.Status.PROCESSING,
        }
        context["export_failed"] = (
            export is not None
            and export.status == ExtractionResultExport.Status.FAILED
            and not context["export_in_progress"]
        )
        row_count = (
            ExtractionInstance.objects.filter(task__job=job).only("id").count()
        )
        context["export_row_count"] = row_count
        small_limit = settings.EXTRACTION_SMALL_EXPORT_ROW_LIMIT
        context["small_export_available"] = (
            context["download_available"] and row_count <= small_limit
        )
        if context["small_export_available"]:
            context["export_in_progress"] = False
            context["export_failed"] = False
        return context


class ExtractionResultDownloadView(ExtractionsLockedMixin, LoginRequiredMixin, View):
    FINISHED_STATUSES = {
        ExtractionJob.Status.SUCCESS,
        ExtractionJob.Status.WARNING,
        ExtractionJob.Status.FAILURE,
    }

    request: AuthenticatedHttpRequest

    def get_queryset(self) -> QuerySet[ExtractionJob]:
        if self.request.user.is_staff:
            return ExtractionJob.objects.all()
        return ExtractionJob.objects.filter(owner=self.request.user)

    def get(self, request: AuthenticatedHttpRequest, pk: int, *args, **kwargs):
        job = get_object_or_404(self.get_queryset(), pk=pk)

        if job.status not in self.FINISHED_STATUSES:
            return HttpResponse(
                "Cannot download results: extraction job has not finished processing.",
                status=409,
                content_type="text/plain",
            )

        output_fields = list(job.output_fields.order_by("id"))
        instances_qs = (
            ExtractionInstance.objects.filter(task__job=job)
            .order_by("id")
            .only("id", "output")
        )
        chunk_size = getattr(settings, "EXTRACTION_RESULTS_EXPORT_CHUNK_SIZE", 1000)
        row_count = instances_qs.count()
        force_refresh = request.GET.get("refresh") == "1"

        small_limit = settings.EXTRACTION_SMALL_EXPORT_ROW_LIMIT
        if row_count <= small_limit:
            response = self._build_inline_response(
                job, output_fields, instances_qs, chunk_size
            )
            return response

        message = "Extraction results are being prepared. Please check back in a moment."
        status_code = 202

        with transaction.atomic():
            latest_export = job.result_exports.select_for_update().first()
            should_enqueue = False

            if (
                latest_export
                and latest_export.status == ExtractionResultExport.Status.COMPLETED
                and latest_export.file
                and not force_refresh
            ):
                try:
                    file_handle = latest_export.file.open("rb")
                except FileNotFoundError:
                    latest_export.status = ExtractionResultExport.Status.FAILED
                    latest_export.error_message = "Export file missing. Regenerating."
                    latest_export.save(update_fields=["status", "error_message"])
                    should_enqueue = True
                    message = "Export file missing. Regenerating."
                else:
                    return FileResponse(
                        file_handle,
                        as_attachment=True,
                        filename=f"extraction-job-{job.pk}.csv",
                        content_type="text/csv",
                    )

            export: ExtractionResultExport
            created = False
            if (
                force_refresh
                or latest_export is None
                or latest_export.status == ExtractionResultExport.Status.FAILED
            ):
                export = job.result_exports.create(requested_by=request.user)
                created = True
            else:
                export = latest_export

            should_enqueue = should_enqueue or created

            if not created and export.status == ExtractionResultExport.Status.FAILED:
                export.status = ExtractionResultExport.Status.PENDING
                export.error_message = ""
                export.save(update_fields=["status", "error_message"])
                should_enqueue = True

            if should_enqueue:
                transaction.on_commit(export.delay)
                message = "Extraction export has been scheduled. Please check back shortly."
            elif export.status == ExtractionResultExport.Status.PROCESSING:
                message = "An export is currently in progress. Please try again later."
            elif export.status == ExtractionResultExport.Status.PENDING:
                message = "An export request is pending. Please try again later."
            else:
                message = "An export request already exists. Please try again later."

        return HttpResponse(message, status=status_code, content_type="text/plain")

    @staticmethod
    def _build_inline_response(
        job: ExtractionJob,
        output_fields: list,
        instances_qs: QuerySet[ExtractionInstance],
        chunk_size: int,
    ) -> HttpResponse:
        buffer = io.StringIO()
        writer = csv.writer(buffer, lineterminator="\n")

        header = ["id"] + [field.name for field in output_fields]
        writer.writerow(header)

        for instance in instances_qs.iterator(chunk_size=chunk_size):
            output_data = instance.output or {}
            row = [sanitize_csv_value(instance.pk)]
            for field in output_fields:
                value = output_data.get(field.name)
                row.append(sanitize_csv_value(value))
            writer.writerow(row)

        content = buffer.getvalue().encode("utf-8")
        response = HttpResponse(content, content_type="text/csv")
        response["Content-Disposition"] = (
            f'attachment; filename="extraction-job-{job.pk}.csv"'
        )
        return response
