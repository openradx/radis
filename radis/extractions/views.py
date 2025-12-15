import csv
from collections.abc import Generator
from datetime import date
from typing import Any, Literal, Type, cast

from adit_radis_shared.accounts.models import User
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
from django.http import StreamingHttpResponse
from django.shortcuts import redirect
from django.urls import reverse_lazy
from django.utils.text import slugify
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
from radis.reports.models import Language, Modality
from radis.search.site import Search, SearchFilters
from radis.search.utils.query_parser import QueryParser

from .filters import ExtractionInstanceFilter, ExtractionJobFilter, ExtractionTaskFilter
from .forms import (
    OutputFieldFormSet,
    SearchForm,
    SummaryForm,
)
from .mixins import ExtractionsLockedMixin
from .models import ExtractionInstance, ExtractionJob, ExtractionTask
from .site import extraction_retrieval_provider
from .tables import (
    ExtractionInstanceTable,
    ExtractionJobTable,
    ExtractionResultsTable,
    ExtractionTaskTable,
)
from .utils.csv_export import iter_extraction_result_rows
from .utils.query_generator import QueryGenerator

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
            context["retrieval_count"] = data.get("retrieval_count", 0)
            context["requires_query_generation"] = data.get("requires_query_generation", False)

            # Store for use in summary step
            self.storage.extra_data["retrieval_count"] = data.get("retrieval_count", 0)
            self.storage.extra_data["requires_query_generation"] = data.get(
                "requires_query_generation", False
            )

        elif self.steps.current == ExtractionJobWizardView.SUMMARY_STEP:
            search_data = self.get_cleaned_data_for_step(ExtractionJobWizardView.SEARCH_STEP)
            output_fields_data = self.get_cleaned_data_for_step(
                ExtractionJobWizardView.OUTPUT_FIELDS_STEP
            )
            assert search_data and isinstance(search_data, dict)
            assert output_fields_data and isinstance(output_fields_data, list)
            # Auto-generate query if needed
            if self.storage.extra_data.get("requires_query_generation", False):
                # Create temporary OutputField objects for query generation
                from .models import OutputField

                temp_fields = [
                    OutputField(
                        name=field_data["name"],
                        description=field_data["description"],
                        output_type=field_data["output_type"],
                    )
                    for field_data in output_fields_data
                    if not field_data.get("DELETE", False)
                ]

                generator = QueryGenerator()
                generated_query, metadata = generator.generate_from_fields(temp_fields)

                # Check if generation failed
                if generated_query is None or not metadata.get("success"):
                    from django.contrib import messages

                    messages.error(
                        self.request,
                        "Unable to automatically generate a query from your extraction fields. "
                        "Please go back to Step 1 and manually enter a search query.",
                    )
                    # Redirect back to step 0
                    self.storage.current_step = self.steps.first
                    return self.render_goto_step(self.steps.first)

                # Store generated query
                search_data["query"] = generated_query
                search_data["query_metadata"] = metadata
                self.storage.extra_data["generated_query"] = generated_query
                self.storage.extra_data["query_metadata"] = metadata

                # Calculate retrieval count for auto-generated query
                # Parse the generated query
                query_node, _ = QueryParser().parse(generated_query)
                assert query_node  # Should be valid since it was just generated and validated

                # Build search with all filters from search_data
                user = cast(User, self.request.user)
                active_group = user.active_group
                assert active_group

                language = cast(Language, search_data.get("language"))
                modalities = cast(QuerySet[Modality], search_data.get("modalities"))

                search = Search(
                    query=query_node,
                    offset=0,
                    limit=0,
                    filters=SearchFilters(
                        group=active_group.pk,
                        language=language.code,
                        modalities=list(modalities.values_list("code", flat=True))
                        if modalities
                        else [],
                        study_date_from=cast("date | None", search_data.get("study_date_from")),
                        study_date_till=cast("date | None", search_data.get("study_date_till")),
                        study_description=cast(str, search_data.get("study_description")) or "",
                        patient_sex=cast(Literal["M", "F"], search_data.get("patient_sex")) or None,
                        patient_age_from=cast("int | None", search_data.get("age_from")),
                        patient_age_till=cast("int | None", search_data.get("age_till")),
                    ),
                )

                # Get the actual count
                if extraction_retrieval_provider is None:
                    from django.contrib import messages

                    messages.error(self.request, "Extraction retrieval provider is not configured.")
                    self.storage.current_step = self.steps.first
                    return self.render_goto_step(self.steps.first)

                retrieval_count = extraction_retrieval_provider.count(search)
                self.storage.extra_data["retrieval_count"] = retrieval_count

                # Validate against limits
                if retrieval_count > settings.EXTRACTION_MAXIMUM_REPORTS_COUNT:
                    from django.contrib import messages

                    messages.error(
                        self.request,
                        f"Your auto-generated query returned {retrieval_count} results, "
                        "which exceeds the maximum limit of "
                        f"{settings.EXTRACTION_MAXIMUM_REPORTS_COUNT}. "
                        "Please go back and refine your extraction fields or add a manual query.",
                    )
                    self.storage.current_step = self.steps.first
                    return self.render_goto_step(self.steps.first)

                if (
                    extraction_retrieval_provider.max_results
                    and retrieval_count > extraction_retrieval_provider.max_results
                ):
                    from django.contrib import messages

                    messages.error(
                        self.request,
                        f"Your auto-generated query returned {retrieval_count} results, "
                        "which exceeds the provider's limit. "
                        "Please refine your extraction fields or add a manual query.",
                    )
                    self.storage.current_step = self.steps.first
                    return self.render_goto_step(self.steps.first)

            context["search"] = search_data
            context["output_fields"] = output_fields_data
            context["retrieval_count"] = self.storage.extra_data.get("retrieval_count", 0)
            context["query_metadata"] = self.storage.extra_data.get("query_metadata", {})

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

            # Use generated query if it was auto-generated
            if self.storage.extra_data.get("requires_query_generation", False):
                generated_query = self.storage.extra_data.get("generated_query", "")
                job_form.instance.query = generated_query

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


class _Echo:
    """Lightweight write-only buffer for csv.writer."""

    def write(self, value: str) -> str:
        return value


class ExtractionResultDownloadView(ExtractionsLockedMixin, LoginRequiredMixin, DetailView):
    """Stream extraction results as a CSV download."""

    model = ExtractionJob
    request: AuthenticatedHttpRequest

    def get_queryset(self) -> QuerySet[ExtractionJob]:
        """Return the accessible extraction jobs for the current user."""
        assert self.model
        model = cast(Type[ExtractionJob], self.model)
        if self.request.user.is_staff:
            return model.objects.all()
        return model.objects.filter(owner=self.request.user)

    def get(self, request: AuthenticatedHttpRequest, *args, **kwargs) -> StreamingHttpResponse:
        """Stream the CSV file response."""
        job = cast(ExtractionJob, self.get_object())
        filename = self._build_filename(job)

        response = StreamingHttpResponse(
            self._stream_rows(job),
            content_type="text/csv",
        )
        response["Content-Disposition"] = f'attachment; filename="{filename}"'
        return response

    def _stream_rows(self, job: ExtractionJob) -> Generator[str, None, None]:
        """Yield serialized CSV rows for the response."""
        pseudo_buffer = _Echo()
        writer = csv.writer(pseudo_buffer)
        yield "\ufeff"
        for row in iter_extraction_result_rows(job):
            yield writer.writerow(row)

    def _build_filename(self, job: ExtractionJob) -> str:
        """Generate a descriptive CSV filename for the extraction job."""
        slug = slugify(job.title) or "results"
        return f"extraction_job_{job.pk}_{slug}.csv"
