from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from django.db.models import QuerySet
from django.views.generic.detail import DetailView

from adit_radis_shared.common.types import AuthenticatedHttpRequest

from .models import Report


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
