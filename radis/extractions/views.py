import csv
from collections.abc import Generator
from datetime import datetime
from typing import Any, Literal, Type, Union, cast
from urllib.parse import urlencode

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
from django.shortcuts import redirect, render
from django.urls import reverse, reverse_lazy
from django.utils.text import slugify
from django.views.generic import DetailView, View
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
    OUTPUT_FIELDS_STEP = "0"
    SEARCH_STEP = "1"
    SUMMARY_STEP = "2"

    form_list = [OutputFieldFormSet, SearchForm, SummaryForm]
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

    def process_step(self, form):
        """Process validated form data and trigger query generation after output fields step."""
        step_data = self.get_form_step_data(form)

        # After output fields are submitted, store field data for async query generation
        if self.steps.current == ExtractionJobWizardView.OUTPUT_FIELDS_STEP:
            # Extract and serialize output fields data for async generation
            formset_data = []
            if hasattr(form, "cleaned_data"):
                formset_data = form.cleaned_data

            output_fields_data = [
                {
                    "name": field_data["name"],
                    "description": field_data["description"],
                    "output_type": field_data["output_type"],
                }
                for field_data in formset_data
                if not field_data.get("DELETE", False)
            ]

            # Store serialized data for async generation - query will be generated via HTMX
            self.storage.extra_data["output_fields_data"] = output_fields_data
            self.storage.extra_data["query_generation_attempted"] = False
            # Clear any previous query to ensure fresh generation
            self.storage.extra_data["generated_query"] = ""
            self.storage.extra_data["query_metadata"] = {}

            return step_data

        # After search step is submitted, store retrieval count for summary
        elif self.steps.current == ExtractionJobWizardView.SEARCH_STEP:
            if hasattr(form, "cleaned_data"):
                retrieval_count = form.cleaned_data.get("retrieval_count")
                if retrieval_count is not None:
                    self.storage.extra_data["retrieval_count"] = retrieval_count

            return step_data

        # For any other steps (SUMMARY_STEP or future additions)
        return step_data

    def get_form_initial(self, step=None):
        """Provide initial data for forms, including generated query for search step."""
        initial = super().get_form_initial(step)

        if step == ExtractionJobWizardView.SEARCH_STEP:
            # Pre-populate query field with generated query
            generated_query = self.storage.extra_data.get("generated_query", "")
            if generated_query:
                initial["query"] = generated_query

        return initial

    def render(self, form=None, **kwargs):
        """Override to validate wizard data integrity before rendering summary step."""
        # Only validate if we're on the summary step
        if self.steps.current == ExtractionJobWizardView.SUMMARY_STEP:
            output_fields_data = self.get_cleaned_data_for_step(
                ExtractionJobWizardView.OUTPUT_FIELDS_STEP
            )
            search_data = self.get_cleaned_data_for_step(ExtractionJobWizardView.SEARCH_STEP)

            # If output fields data is missing or invalid, restart wizard
            if not output_fields_data or not isinstance(output_fields_data, list):
                from django.contrib import messages

                self.storage.reset()
                self.storage.current_step = self.steps.first
                messages.error(
                    self.request,
                    "Wizard data was lost or corrupted. Please start over from step 1.",
                )
                return redirect(reverse("extraction_job_create"))

            # If search data is missing, go back to search step
            if not search_data or not isinstance(search_data, dict):
                from django.contrib import messages

                self.storage.current_step = ExtractionJobWizardView.SEARCH_STEP
                messages.error(
                    self.request,
                    "Search data is missing. Please complete step 2.",
                )
                return redirect(reverse("extraction_job_create"))

        return super().render(form, **kwargs)

    def get_context_data(self, form, **kwargs):
        context = super().get_context_data(form, **kwargs)

        if self.steps.current == ExtractionJobWizardView.OUTPUT_FIELDS_STEP:
            # First step - just show the formset
            context["formset"] = form

        elif self.steps.current == ExtractionJobWizardView.SEARCH_STEP:
            # Second step - show generated query info and retrieval count
            context["generated_query"] = self.storage.extra_data.get("generated_query", "")
            context["query_metadata"] = self.storage.extra_data.get("query_metadata", {})

            # Get output fields data to show context
            output_fields_data = self.get_cleaned_data_for_step(
                ExtractionJobWizardView.OUTPUT_FIELDS_STEP
            )
            if output_fields_data:
                context["output_fields_count"] = len(
                    [
                        f
                        for f in output_fields_data
                        if not cast(dict[str, Any], f).get("DELETE", False)
                    ]
                )

        elif self.steps.current == ExtractionJobWizardView.SUMMARY_STEP:
            # Final step - show everything for review
            # Data integrity validated in render() method
            output_fields_data = self.get_cleaned_data_for_step(
                ExtractionJobWizardView.OUTPUT_FIELDS_STEP
            )
            search_data = self.get_cleaned_data_for_step(ExtractionJobWizardView.SEARCH_STEP)

            context["output_fields"] = output_fields_data
            context["search"] = search_data
            context["retrieval_count"] = self.storage.extra_data.get("retrieval_count", 0)
            context["query_metadata"] = self.storage.extra_data.get("query_metadata", {})

        return context

    def get_template_names(self) -> list[str]:
        step = self.steps.current
        if step == ExtractionJobWizardView.OUTPUT_FIELDS_STEP:
            return ["extractions/extraction_job_output_fields_form.html"]
        elif step == ExtractionJobWizardView.SEARCH_STEP:
            return ["extractions/extraction_job_search_form.html"]
        elif step == ExtractionJobWizardView.SUMMARY_STEP:
            return ["extractions/extraction_job_wizard_summary.html"]
        else:
            raise SuspiciousOperation(f"Invalid wizard step: {step}")

    def done(
        self,
        form_objs: tuple[BaseInlineFormSet, SearchForm, SummaryForm],
        **kwargs,
    ):
        user = self.request.user

        with transaction.atomic():
            output_fields_formset = form_objs[0]
            search_form = form_objs[1]
            summary_form = form_objs[2]

            # The query is always in search_form now (no conditional logic needed)
            if summary_form.cleaned_data["send_finished_mail"]:
                search_form.cleaned_data["send_finished_mail"] = True

            job: ExtractionJob = search_form.save(commit=False)

            # Parse and normalize the query
            query = job.query
            query_node, fixes = QueryParser().parse(query)
            if len(fixes) > 0:
                assert query_node
                job.query = QueryParser.unparse(query_node)

            group = user.active_group
            assert group

            job.group = group
            job.owner = user
            job.save()

            # Save output fields
            output_fields_formset.instance = job
            output_fields_formset.save()

            if user.is_staff or settings.START_EXTRACTION_JOB_UNVERIFIED:
                job.status = ExtractionJob.Status.PENDING
                job.save()

                transaction.on_commit(lambda: job.delay())

        return redirect(job)


class ExtractionSearchPreviewView(LoginRequiredMixin, View):
    """HTMX endpoint for live search preview: count and link."""

    request: AuthenticatedHttpRequest

    def get(self, request: AuthenticatedHttpRequest):
        # Wizard step prefix for form field names
        WIZARD_STEP_PREFIX = "1-"

        # Extract query and filter parameters from GET
        query_str = request.GET.get(f"{WIZARD_STEP_PREFIX}query", "").strip()
        language_id = request.GET.get(f"{WIZARD_STEP_PREFIX}language")
        modality_ids = request.GET.getlist(f"{WIZARD_STEP_PREFIX}modalities")

        # Get string values from GET parameters
        study_date_from_str = request.GET.get(f"{WIZARD_STEP_PREFIX}study_date_from", "").strip()
        study_date_till_str = request.GET.get(f"{WIZARD_STEP_PREFIX}study_date_till", "").strip()
        study_description = request.GET.get(f"{WIZARD_STEP_PREFIX}study_description", "")
        patient_sex_str = request.GET.get(f"{WIZARD_STEP_PREFIX}patient_sex")
        patient_sex: Literal["M", "F"] | None = None
        if patient_sex_str in ("M", "F"):
            patient_sex = patient_sex_str  # Type narrowed to Literal["M", "F"]
        age_from_str = request.GET.get(f"{WIZARD_STEP_PREFIX}age_from", "").strip()
        age_till_str = request.GET.get(f"{WIZARD_STEP_PREFIX}age_till", "").strip()

        # Parse dates from YYYY-MM-DD format to date objects
        study_date_from = None
        study_date_till = None
        if study_date_from_str:
            try:
                study_date_from = datetime.strptime(study_date_from_str, "%Y-%m-%d").date()
            except ValueError:
                pass  # Invalid date format, leave as None

        if study_date_till_str:
            try:
                study_date_till = datetime.strptime(study_date_till_str, "%Y-%m-%d").date()
            except ValueError:
                pass  # Invalid date format, leave as None

        # Parse age values to integers
        age_from_int = None
        age_till_int = None
        if age_from_str:
            try:
                age_from_int = int(age_from_str)
            except ValueError:
                pass  # Invalid integer, leave as None

        if age_till_str:
            try:
                age_till_int = int(age_till_str)
            except ValueError:
                pass  # Invalid integer, leave as None

        # Get user's active group
        user = cast("User", request.user)
        active_group = user.active_group

        # Validate query syntax
        if not query_str:
            context = {
                "count": None,
                "search_url": None,
                "error": None,
                "max_reports_limit": settings.EXTRACTION_MAXIMUM_REPORTS_COUNT,
            }
            return render(request, "extractions/_search_preview.html", context)

        query_node, fixes = QueryParser().parse(query_str)
        if query_node is None:
            context = {
                "count": None,
                "search_url": None,
                "error": "Invalid query syntax",
                "max_reports_limit": settings.EXTRACTION_MAXIMUM_REPORTS_COUNT,
            }
            return render(request, "extractions/_search_preview.html", context)

        # Get language and modalities objects
        try:
            language = Language.objects.get(pk=language_id) if language_id else None
            modalities_qs = (
                Modality.objects.filter(pk__in=modality_ids)
                if modality_ids
                else Modality.objects.none()
            )
        except (Language.DoesNotExist, ValueError):
            context = {
                "count": None,
                "search_url": None,
                "error": "Invalid language or modality selection",
                "max_reports_limit": settings.EXTRACTION_MAXIMUM_REPORTS_COUNT,
            }
            return render(request, "extractions/_search_preview.html", context)

        # Build search object (age_from_int and age_till_int already parsed above)
        search = Search(
            query=query_node,
            offset=0,
            limit=0,
            filters=SearchFilters(
                group=active_group.pk if active_group else None,
                language=language.code if language else "",
                modalities=list(modalities_qs.values_list("code", flat=True)),
                study_date_from=study_date_from,
                study_date_till=study_date_till,
                study_description=study_description,
                patient_sex=patient_sex,
                patient_age_from=age_from_int,
                patient_age_till=age_till_int,
            ),
        )

        # Calculate count
        if extraction_retrieval_provider is None:
            context = {
                "count": None,
                "search_url": None,
                "error": "Extraction retrieval provider is not configured",
                "max_reports_limit": settings.EXTRACTION_MAXIMUM_REPORTS_COUNT,
            }
            return render(request, "extractions/_search_preview.html", context)

        retrieval_count = extraction_retrieval_provider.count(search)

        # Generate search URL - use STRING values for URL parameters
        # Note: modalities can be a list, which urlencode with doseq=True expands
        search_params: dict[str, Union[str, list[str]]] = {"query": query_str}
        if language_id:
            search_params["language"] = Language.objects.get(pk=language_id).code
        if modality_ids:
            search_params["modalities"] = modality_ids
        if study_date_from_str:  # Use STRING version for URL
            search_params["study_date_from"] = study_date_from_str
        if study_date_till_str:  # Use STRING version for URL
            search_params["study_date_till"] = study_date_till_str
        if study_description:
            search_params["study_description"] = study_description
        if patient_sex:
            search_params["patient_sex"] = patient_sex
        if age_from_str:  # Use STRING version for URL
            search_params["age_from"] = age_from_str
        if age_till_str:  # Use STRING version for URL
            search_params["age_till"] = age_till_str

        # doseq=True ensures modalities list becomes: modalities=DX&modalities=MR
        search_url = reverse("search") + "?" + urlencode(search_params, doseq=True)

        context = {
            "count": retrieval_count,
            "search_url": search_url,
            "error": None,
            "max_reports_limit": settings.EXTRACTION_MAXIMUM_REPORTS_COUNT,
        }
        return render(request, "extractions/_search_preview.html", context)


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


class ExtractionQueryGeneratorView(LoginRequiredMixin, View):
    """HTMX endpoint for async query generation from output fields."""

    request: AuthenticatedHttpRequest

    async def post(self, request: AuthenticatedHttpRequest):
        """Generate query asynchronously and save to wizard session."""
        import logging

        logger = logging.getLogger(__name__)
        logger.info("Query generation endpoint called")

        # Access wizard session storage
        # Django-formtools stores wizard data in a nested structure:
        # session['wizard_extraction_job_wizard_view'] = {
        #     'step': '1',
        #     'step_data': {...},
        #     'extra_data': {'output_fields_data': [...], ...}
        # }
        wizard_session_key = "wizard_extraction_job_wizard_view"
        wizard_data = request.session.get(wizard_session_key, {})

        if not wizard_data:
            logger.error(f"No wizard session data found for key: {wizard_session_key}")
            logger.error(f"Available session keys: {list(request.session.keys())}")

        # Get extra_data from within the wizard data
        extra_data = wizard_data.get("extra_data", {})
        output_fields_data = extra_data.get("output_fields_data", [])

        logger.info(f"Found wizard_data: {bool(wizard_data)}, extra_data: {bool(extra_data)}")
        logger.info(f"Found {len(output_fields_data)} output fields in session")

        if not output_fields_data:
            context = {
                "error": "No output fields found. Please go back to step 1.",
                "generated_query": "",
                "query_metadata": {},
            }
            return render(request, "extractions/_query_generation_result.html", context)

        # Reconstruct OutputField objects from stored data
        from .models import OutputField

        temp_fields = [
            OutputField(
                name=field_data["name"],
                description=field_data["description"],
                output_type=field_data["output_type"],
            )
            for field_data in output_fields_data
        ]

        # Generate query using async query generator
        from .utils.query_generator import AsyncQueryGenerator

        try:
            generator = AsyncQueryGenerator()
            generated_query, metadata = await generator.generate_from_fields(temp_fields)

            # Store in wizard session
            extra_data["generated_query"] = generated_query or ""
            extra_data["query_metadata"] = metadata
            extra_data["query_generation_attempted"] = True

            # Update wizard data and save back to session
            wizard_data["extra_data"] = extra_data
            request.session[wizard_session_key] = wizard_data
            request.session.modified = True
            logger.info("Saved query to wizard session")

            context = {
                "generated_query": generated_query,
                "query_metadata": metadata,
                "output_fields_count": len(temp_fields),
                "error": None if metadata.get("success") else "Query generation failed",
            }

        except Exception as e:
            logger.error(f"Error during async query generation: {e}", exc_info=True)
            context = {
                "error": f"Error generating query: {str(e)}",
                "generated_query": "",
                "query_metadata": {"success": False, "error": str(e)},
            }
            extra_data["generated_query"] = ""
            extra_data["query_metadata"] = context["query_metadata"]
            extra_data["query_generation_attempted"] = True
            wizard_data["extra_data"] = extra_data
            request.session[wizard_session_key] = wizard_data
            request.session.modified = True

        return render(request, "extractions/_query_generation_result.html", context)


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
