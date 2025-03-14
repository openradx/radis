import django_filters
from adit_radis_shared.common.forms import SingleFilterFieldFormHelper
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

        self.form.helper = SingleFilterFieldFormHelper(self.request.GET, "is_processed")
