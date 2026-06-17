from adit_radis_shared.common.mixins import PageSizeSelectMixin
from adit_radis_shared.common.types import AuthenticatedHttpRequest
from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from django.db.models import QuerySet
from django.utils import timezone
from django.views.generic.base import TemplateView
from django.views.generic.detail import DetailView
from django_filters.views import FilterView

from .filters import ReportFilter
from .models import (
    Report,
    ReportLanguageStat,
    ReportModalityStat,
    ReportOverviewTotal,
    ReportYearStat,
)


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
        return super().get_queryset().filter(groups=active_group)


class ReportBodyView(ReportDetailView):
    template_name = "reports/report_body.html"


class ReportOverviewView(LoginRequiredMixin, UserPassesTestMixin, TemplateView):
    template_name = "reports/report_overview.html"
    permission_denied_message = "You must be logged in and have an active group"
    request: AuthenticatedHttpRequest

    def test_func(self) -> bool | None:
        return self.request.user.active_group is not None

    def get_context_data(self, **kwargs: object) -> dict[str, object]:
        context = super().get_context_data(**kwargs)
        active_group = self.request.user.active_group
        assert active_group

        total_entry = ReportOverviewTotal.objects.filter(group=active_group).first()
        total_count = total_entry.total_count if total_entry else 0

        now = timezone.now()
        last_year = now.year - 1
        prev_year = now.year - 2
        year_counts_by_year = dict(
            ReportYearStat.objects.filter(group=active_group).values_list("year", "count")
        )
        last_year_count = year_counts_by_year.get(last_year, 0)
        prev_year_count = year_counts_by_year.get(prev_year, 0)
        yoy_change = None
        if prev_year_count:
            yoy_change = ((last_year_count - prev_year_count) / prev_year_count) * 100

        year_window = 10
        start_year = last_year - (year_window - 1)
        year_range = list(range(start_year, last_year + 1))
        year_counts = [
            {"year": year, "count": year_counts_by_year.get(year, 0)} for year in year_range
        ]

        modality_counts = list(
            ReportModalityStat.objects.filter(group=active_group).order_by("-count")
        )
        top_modalities = modality_counts[:5]
        other_modality_count = sum(item.count for item in modality_counts[5:])

        language_counts = list(
            ReportLanguageStat.objects.filter(group=active_group).order_by("-count")
        )
        top_languages = language_counts[:10]
        other_language_count = sum(item.count for item in language_counts[10:])

        stats_ready = total_entry is not None
        data = {
            "total_count": total_count,
            "last_year": last_year,
            "prev_year": prev_year,
            "last_year_count": last_year_count,
            "prev_year_count": prev_year_count,
            "yoy_change": yoy_change,
            "year_counts": year_counts,
            "top_modalities": top_modalities,
            "other_modality_count": other_modality_count,
            "top_languages": top_languages,
            "other_language_count": other_language_count,
            "stats_ready": stats_ready,
        }
        context.update(data)
        return context
