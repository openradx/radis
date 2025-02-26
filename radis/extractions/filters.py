import django_filters
from adit_radis_shared.common.forms import FilterSetFormHelper
from django.http import HttpRequest

from radis.core.filters import AnalysisJobFilter, AnalysisTaskFilter

from .models import ExtractionInstance, ExtractionJob, ExtractionTask


class ExtractionJobFilter(AnalysisJobFilter):
    class Meta(AnalysisJobFilter.Meta):
        model = ExtractionJob


class ExtractionTaskFilter(AnalysisTaskFilter):
    class Meta(AnalysisTaskFilter.Meta):
        model = ExtractionTask


class ExtractionInstanceFilter(django_filters.FilterSet):
    request: HttpRequest

    class Meta:
        model = ExtractionInstance
        fields = ("is_processed",)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        form_helper = FilterSetFormHelper(self.request.GET)
        form_helper.add_filter_field("is_processed", "select", "Filter")
        form_helper.build_filter_set_layout()
        self.form.helper = form_helper
