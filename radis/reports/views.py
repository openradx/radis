from adit_radis_shared.common.mixins import PageSizeSelectMixin
from adit_radis_shared.common.types import AuthenticatedHttpRequest
from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from django.db.models import Prefetch, QuerySet
from django.views.generic.detail import DetailView
from django_filters.views import FilterView

from radis.labels.models import Answer, LabelingRun

from .filters import ReportFilter
from .models import Report


class ReportListView(LoginRequiredMixin, PageSizeSelectMixin, FilterView):
    template_name = "reports/report_list.html"
    context_object_name = "reports"
    filterset_class = ReportFilter
    paginate_by = 10
    page_sizes = [10, 25, 50]
    request: AuthenticatedHttpRequest

    def get_queryset(self) -> QuerySet[Report]:
        return Report.objects.filter(groups=self.request.user.active_group).order_by(
            "-study_datetime"
        )


class ReportDetailView(LoginRequiredMixin, UserPassesTestMixin, DetailView):
    model = Report
    template_name = "reports/report_detail.html"
    context_object_name = "report"
    permission_denied_message = "You must be logged in and have an active group"
    request: AuthenticatedHttpRequest

    def test_func(self) -> bool | None:
        return self.request.user.active_group is not None

    def get_queryset(self) -> QuerySet[Report]:
        active_group = self.request.user.active_group
        assert active_group
        # Show only DIRECT-mode answers in the canonical report view. REASONED
        # answers are kept for evaluation but aren't the user-facing labels.
        answers_queryset = (
            Answer.objects.filter(run__mode=LabelingRun.Mode.DIRECT)
            .select_related("question__question_set", "option", "run")
            .order_by(
                "question__question_set__order",
                "question__order",
                "question__label",
                "-created_at",
            )
        )
        return (
            super()
            .get_queryset()
            .filter(groups=active_group)
            .prefetch_related(Prefetch("answers", queryset=answers_queryset))
        )


class ReportBodyView(ReportDetailView):
    template_name = "reports/report_body.html"
