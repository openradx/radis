import django_filters
from django.http import HttpRequest

from adit_radis_shared.common.forms import FilterSetFormHelper

from .models import AnalysisJob, AnalysisTask


class AnalysisJobFilter(django_filters.FilterSet):
    request: HttpRequest

    class Meta:
        model: type[AnalysisJob]
        fields = ("status",)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        form_helper = FilterSetFormHelper(self.request.GET)
        form_helper.add_filter_field("status", "select", "Filter")
        form_helper.build_filter_set_layout()
        self.form.helper = form_helper


class AnalysisTaskFilter(django_filters.FilterSet):
    request: HttpRequest

    class Meta:
        model: type[AnalysisTask]
        fields = ("status",)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        form_helper = FilterSetFormHelper(self.request.GET)
        form_helper.add_filter_field("status", "select", "Filter")
        form_helper.build_filter_set_layout()
        self.form.helper = form_helper
