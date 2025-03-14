import django_filters
from adit_radis_shared.common.forms import SingleFilterFieldFormHelper
from django.http import HttpRequest

from .models import AnalysisJob, AnalysisTask


class AnalysisJobFilter(django_filters.FilterSet):
    request: HttpRequest

    class Meta:
        model: type[AnalysisJob]
        fields = ("status",)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.form.helper = SingleFilterFieldFormHelper(self.request.GET, "status")


class AnalysisTaskFilter(django_filters.FilterSet):
    request: HttpRequest

    class Meta:
        model: type[AnalysisTask]
        fields = ("status",)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.form.helper = SingleFilterFieldFormHelper(self.request.GET, "status")
