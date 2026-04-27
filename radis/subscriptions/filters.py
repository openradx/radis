import django_filters
from adit_radis_shared.common.forms import SingleFilterFieldFormHelper
from adit_radis_shared.common.types import with_form_helper
from crispy_forms.helper import FormHelper
from crispy_forms.layout import HTML, Div, Field, Layout, Submit
from django import forms
from django.http import HttpRequest

from radis.reports.models import Modality

from .models import SubscribedItem, Subscription


class SubscriptionFilter(django_filters.FilterSet):
    name = django_filters.CharFilter(lookup_expr="search")
    request: HttpRequest

    class Meta:
        model = Subscription
        fields = ("name",)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        with_form_helper(self.form).helper = SingleFilterFieldFormHelper(self.request.GET, "name")


class SubscribedItemFilter(django_filters.FilterSet):
    patient_id = django_filters.CharFilter(
        label="Patient ID",
        field_name="report__patient_id",
        lookup_expr="icontains",
    )
    study_description = django_filters.CharFilter(
        label="Study Description",
        field_name="report__study_description",
        lookup_expr="icontains",
    )
    study_date_from = django_filters.DateFilter(
        label="Study Date From",
        field_name="report__study_datetime",
        lookup_expr="date__gte",
        widget=forms.DateInput(attrs={"type": "date"}),
    )
    study_date_till = django_filters.DateFilter(
        label="Study Date Till",
        field_name="report__study_datetime",
        lookup_expr="date__lte",
        widget=forms.DateInput(attrs={"type": "date"}),
    )
    modalities = django_filters.ModelMultipleChoiceFilter(
        queryset=Modality.objects.order_by("code"),
        field_name="report__modalities__code",
        to_field_name="code",
    )
    request: HttpRequest

    class Meta:
        model = SubscribedItem
        fields = ()

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        form_helper = FormHelper()
        form_helper.form_tag = False
        form_helper.disable_csrf = True
        form_helper.layout = Layout(
            Field("patient_id", css_class="form-control-sm"),
            Field("study_description", css_class="form-control-sm"),
            Field("study_date_from", css_class="form-control-sm"),
            Field("study_date_till", css_class="form-control-sm"),
            Field("modalities", css_class="form-select-sm"),
            Div(
                Submit("submit", "Apply Filters", css_class="btn btn-sm btn-primary"),
                HTML("<a class='btn btn-sm btn-secondary' href='{{ request.path }}'>Reset</a>"),
                css_class="d-flex justify-content-center gap-2",
            ),
        )
        with_form_helper(self.form).helper = form_helper
