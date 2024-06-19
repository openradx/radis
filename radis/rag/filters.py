import django_filters
from adit_radis_shared.common.forms import FilterSetFormHelper
from django.http import HttpRequest

from radis.core.filters import AnalysisJobFilter, AnalysisTaskFilter

from .models import RagJob, RagReportInstance, RagTask


class RagJobFilter(AnalysisJobFilter):
    class Meta(AnalysisJobFilter.Meta):
        model = RagJob


class RagTaskFilter(AnalysisTaskFilter):
    class Meta(AnalysisTaskFilter.Meta):
        model = RagTask


class RagResultFilter(django_filters.FilterSet):
    request: HttpRequest

    class Meta:
        model = RagReportInstance
        fields = ("overall_result",)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        form_helper = FilterSetFormHelper(self.request.GET)
        form_helper.add_filter_field("overall_result", "select", "Filter")
        form_helper.build_filter_set_layout()
        self.form.helper = form_helper
