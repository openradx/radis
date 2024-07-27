from typing import Any, Type, cast

from adit_radis_shared.common.mixins import (
    HtmxOnlyMixin,
    PageSizeSelectMixin,
    RelatedFilterMixin,
    RelatedPaginationMixin,
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
from django.shortcuts import redirect, render
from django.urls import reverse_lazy
from django.views.generic import DetailView, View
from django_tables2 import SingleTableMixin
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
from radis.rag.filters import RagJobFilter, RagResultFilter, RagTaskFilter
from radis.rag.mixins import RagLockedMixin
from radis.rag.tables import RagInstanceTable, RagJobTable, RagTaskTable
from radis.reports.models import Language, Modality
from radis.search.site import Search, SearchFilters
from radis.search.utils.query_parser import QueryParser

from .forms import QuestionFormSet, QuestionFormSetHelper, SearchForm
from .models import Answer, QuestionResult, RagInstance, RagJob, RagTask
from .site import retrieval_providers

RAG_SEARCH_PROVIDER = "rag_search_provider"


class RagUpdatePreferencesView(RagLockedMixin, BaseUpdatePreferencesView):
    allowed_keys = [
        RAG_SEARCH_PROVIDER,
    ]


class RagJobListView(RagLockedMixin, AnalysisJobListView):
    model = RagJob
    table_class = RagJobTable
    filterset_class = RagJobFilter
    template_name = "rag/rag_job_list.html"


class RagJobWizardView(
    LoginRequiredMixin, PermissionRequiredMixin, UserPassesTestMixin, SessionWizardView
):
    SEARCH_STEP = "0"
    QUESTIONS_STEP = "1"

    form_list = [SearchForm, QuestionFormSet]
    permission_required = "rag.add_ragjob"
    permission_denied_message = "You must be logged in and have an active group"
    request: AuthenticatedHttpRequest

    def test_func(self) -> bool | None:
        return self.request.user.active_group is not None

    def get_form_kwargs(self, step=None):
        kwargs = super().get_form_kwargs(step)
        if step == RagJobWizardView.SEARCH_STEP:
            kwargs["user"] = self.request.user
        return kwargs

    def get_context_data(self, form, **kwargs):
        context = super().get_context_data(form, **kwargs)

        if self.steps.current == RagJobWizardView.QUESTIONS_STEP:
            context["helper"] = QuestionFormSetHelper()

            data = self.get_cleaned_data_for_step(RagJobWizardView.SEARCH_STEP)
            assert data
            context["provider"] = retrieval_providers[data["provider"]]

            active_group = self.request.user.active_group
            assert active_group

            language = cast(Language, data["language"])
            modalities = cast(QuerySet[Modality], data["modalities"])

            query_node, fixes = QueryParser().parse(data["query"])

            # It was already validated by the form in the first step that the query is not empty
            assert query_node

            if len(fixes) > 0:
                context["fixed_query"] = QueryParser.unparse(query_node)

            search = Search(
                query=query_node,
                offset=0,
                limit=0,
                filters=SearchFilters(
                    group=active_group.pk,
                    language=language.code,
                    modalities=list(modalities.values_list("code", flat=True)),
                    study_date_from=data["study_date_from"],
                    study_date_till=data["study_date_till"],
                    study_description=data["study_description"],
                    patient_sex=data["patient_sex"],
                    patient_age_from=data["age_from"],
                    patient_age_till=data["age_till"],
                ),
            )

            retrieval_provider = retrieval_providers[data["provider"]]
            context["retrieval_count"] = retrieval_provider.count(search)

        return context

    def get_template_names(self) -> list[str]:
        step = self.steps.current
        if step == RagJobWizardView.SEARCH_STEP:
            return ["rag/rag_job_search_form.html"]
        elif step == RagJobWizardView.QUESTIONS_STEP:
            return ["rag/rag_job_questions_form.html"]
        else:
            raise SuspiciousOperation(f"Invalid wizard step: {step}")

    def done(self, form_objs: tuple[SearchForm, BaseInlineFormSet], **kwargs):
        user = self.request.user

        with transaction.atomic():
            job: RagJob = form_objs[0].save(commit=False)

            # We save the fixed query to the model. The user was already warned
            # about this in the questions step.
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

            form_objs[1].instance = job
            form_objs[1].save()

            if user.is_staff or settings.START_RAG_JOB_UNVERIFIED:
                job.status = RagJob.Status.PREPARING
                job.save()

                transaction.on_commit(lambda: job.delay())

        return redirect(job)


class RagJobDetailView(AnalysisJobDetailView):
    model = RagJob
    table_class = RagTaskTable
    filterset_class = RagTaskFilter
    context_object_name = "job"
    template_name = "rag/rag_job_detail.html"


class RagJobDeleteView(RagLockedMixin, AnalysisJobDeleteView):
    model = RagJob
    success_url = reverse_lazy("rag_job_list")


class RagJobVerifyView(RagLockedMixin, AnalysisJobVerifyView):
    model = RagJob


class RagJobCancelView(RagLockedMixin, AnalysisJobCancelView):
    model = RagJob


class RagJobResumeView(RagLockedMixin, AnalysisJobResumeView):
    model = RagJob


class RagJobRetryView(RagLockedMixin, AnalysisJobRetryView):
    model = RagJob


class RagJobRestartView(RagLockedMixin, AnalysisJobRestartView):
    model = RagJob


class RagTaskDetailView(RagLockedMixin, AnalysisTaskDetailView, SingleTableMixin):
    model = RagTask
    table_class = RagInstanceTable
    filterset_class = RagResultFilter
    job_url_name = "rag_job_detail"
    template_name = "rag/rag_task_detail.html"

    def get_context_data(self, **kwargs) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        task = cast(RagTask, self.get_object())
        self.object_list = task.rag_instances.all()
        table = self.get_table()
        context[self.get_context_table_name(table)] = table
        return context


class RagTaskDeleteView(RagLockedMixin, AnalysisTaskDeleteView):
    model = RagTask


class RagTaskResetView(RagLockedMixin, AnalysisTaskResetView):
    model = RagTask


class RagResultListView(
    RagLockedMixin,
    LoginRequiredMixin,
    RelatedPaginationMixin,
    RelatedFilterMixin,
    PageSizeSelectMixin,
    DetailView,
):
    filterset_class = RagResultFilter
    model = RagJob
    context_object_name = "job"
    template_name = "rag/rag_result_list.html"
    request: AuthenticatedHttpRequest
    object_list: QuerySet[RagInstance]

    def get_queryset(self) -> QuerySet[RagJob]:
        assert self.model
        model = cast(Type[RagJob], self.model)
        if self.request.user.is_staff:
            return model.objects.all()
        return model.objects.filter(owner=self.request.user)

    def get_related_queryset(self) -> QuerySet[RagInstance]:
        job = cast(RagJob, self.get_object())
        rag_instances = RagInstance.objects.filter(
            task__job=job,
            overall_result__in=[
                RagInstance.Result.ACCEPTED,
                RagInstance.Result.REJECTED,
            ],
        ).prefetch_related("reports")
        return rag_instances

    def get_filter_queryset(self) -> QuerySet[RagInstance]:
        return self.get_related_queryset()


class ChangeAnswerView(LoginRequiredMixin, HtmxOnlyMixin, View):
    def post(self, request: AuthenticatedHttpRequest, result_id: int):
        user = request.user

        result = QuestionResult.objects.get(id=result_id)
        question = result.question
        rag_instance = result.rag_instance

        if rag_instance.task.job.owner != user:
            raise SuspiciousOperation("You are not the owner of this task")

        with transaction.atomic():
            result.current_answer = Answer.NO if result.current_answer == Answer.YES else Answer.YES
            if result.current_answer == question.accepted_answer:
                result.result = RagInstance.Result.ACCEPTED
            else:
                result.result = RagInstance.Result.REJECTED
            result.save()

            all_results = list(rag_instance.results.values_list("result", flat=True))
            if all(result == RagInstance.Result.ACCEPTED for result in all_results):
                rag_instance.overall_result = RagInstance.Result.ACCEPTED
            else:
                rag_instance.overall_result = RagInstance.Result.REJECTED
            rag_instance.save()

        return render(
            request,
            "rag/_changed_result.html",
            {
                "rag_instance": rag_instance,
                "result": result,
            },
        )


class RagInstanceDetailView(LoginRequiredMixin, DetailView):
    model: type[RagInstance]
    task_url_name: str = "rag_task_detail"
    job_url_name: str = "rag_job_detail"
    context_object_name = "rag_instance"
    template_name: str = "rag/rag_instance_detail.html"
    request: AuthenticatedHttpRequest

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        context["task_url_name"] = self.task_url_name
        context["job_url_name"] = self.job_url_name
        return context

    def get_queryset(self) -> QuerySet[RagInstance]:
        if self.request.user.is_staff:
            return RagInstance.objects.all()
        return RagInstance.objects.filter(task__job__owner=self.request.user)
