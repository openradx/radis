import django_filters
from crispy_forms.helper import FormHelper
from crispy_forms.layout import HTML, Div, Field, Layout, Submit
from django import forms
from django.http import HttpRequest

from .models import Modality, Report


class ReportFilter(django_filters.FilterSet):
    patient_id = django_filters.CharFilter(label="Patient ID", lookup_expr="exact")
    modalities = django_filters.ModelMultipleChoiceFilter(
        queryset=Modality.objects.order_by("code"),
        field_name="modalities__code",
        to_field_name="code",
    )
    study_date_from = django_filters.DateFilter(
        label="Study date from",
        field_name="study_datetime",
        lookup_expr="date__gte",
        widget=forms.DateInput(attrs={"type": "date"}),
    )
    study_date_till = django_filters.DateFilter(
        label="Study date till",
        field_name="study_datetime",
        lookup_expr="date__lte",
        widget=forms.DateInput(attrs={"type": "date"}),
    )
    study_description = django_filters.CharFilter(
        label="Study description", lookup_expr="icontains"
    )
    request: HttpRequest

    class Meta:
        model = Report
        fields = ("patient_id",)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.form.helper = FormHelper()
        self.form.helper.form_tag = False
        self.form.helper.disable_csrf = True

        self.form.helper.layout = Layout(
            Field("patient_id", css_class="form-control-sm"),
            Field("modalities", css_class="form-select-sm"),
            Field("study_date_from", css_class="form-control-sm"),
            Field("study_date_till", css_class="form-control-sm"),
            Field("study_description", css_class="form-control-sm"),
            Div(
                Submit("submit", "Submit", css_class="btn btn-sm btn-primary"),
                HTML(
                    "<a class='btn btn-sm btn-secondary' href='{% url 'report_list' %}'>Reset</a>"
                ),
                css_class="d-flex justify-content-center gap-2",
            ),
        )
