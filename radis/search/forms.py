from typing import Any

from crispy_forms.helper import FormHelper, Layout
from crispy_forms.layout import Button, Div, Field
from django import forms
from django.db.models import F, Func
from django.urls import reverse
from django.utils.functional import lazy

from radis.reports.models import Report
from radis.search.layouts import QueryInput, RangeSlider

from .site import search_providers

MIN_AGE = 0
MAX_AGE = 120
AGE_STEP = 10


def get_search_providers():
    return sorted([(provider.name, provider.name) for provider in search_providers.values()])


def get_initial_provider():
    return get_search_providers()[0][0]


class SearchForm(forms.Form):
    # Query fields
    query = forms.CharField(required=False, label=False)
    provider = forms.ChoiceField(
        required=False,
        # TODO: in Django 5 choices and initial can be passed a function directly
        choices=lazy(get_search_providers, tuple)(),
        initial=lazy(get_initial_provider, str)(),
        label=False,
    )
    # Filter fields
    study_date_from = forms.DateField(
        required=False, widget=forms.DateInput(attrs={"type": "date"})
    )
    study_date_till = forms.DateField(
        required=False, widget=forms.DateInput(attrs={"type": "date"})
    )
    study_description = forms.CharField(required=False)
    modalities = forms.MultipleChoiceField(required=False, choices=[])
    patient_sex = forms.ChoiceField(
        required=False, choices=[("", "All"), ("M", "Male"), ("F", "Female")]
    )
    age_from = forms.IntegerField(
        required=False,
        min_value=MIN_AGE,
        max_value=MAX_AGE - AGE_STEP,
        widget=forms.NumberInput(
            attrs={
                "type": "range",
                "min": MIN_AGE,
                "max": MAX_AGE - AGE_STEP,
                "value": MIN_AGE,
                "step": AGE_STEP,
            }
        ),
    )
    age_till = forms.IntegerField(
        required=False,
        min_value=MIN_AGE + AGE_STEP,
        max_value=MAX_AGE,
        widget=forms.NumberInput(
            attrs={
                "type": "range",
                "min": MIN_AGE + AGE_STEP,
                "max": MAX_AGE,
                "value": MAX_AGE,
                "step": AGE_STEP,
            }
        ),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        # TODO: put an index to modalities_in_study
        # https://stackoverflow.com/a/4059785/166229
        modalities = (
            Report.objects.annotate(modalities=Func(F("modalities_in_study"), function="unnest"))
            .values_list("modalities", flat=True)
            .distinct()
        )
        modalities = sorted(modalities)
        modality_choices = [(m, m) for m in modalities]
        self.fields["modalities"].choices = modality_choices

        self.query_helper = FormHelper()
        self.query_helper.template = "search/form_elements/form_part.html"  # type: ignore
        self.query_helper.form_show_errors
        self.query_helper.form_tag = False
        self.query_helper.disable_csrf = True
        self.query_helper.layout = self.create_query_layout()

        self.filters_helper = FormHelper()
        self.filters_helper.template = "search/form_elements/form_part.html"  # type: ignore
        self.filters_helper.form_tag = False
        self.filters_helper.disable_csrf = True
        self.filters_helper.layout = self.create_filters_layout()

    def create_query_layout(self) -> Layout:
        return Layout(
            QueryInput(
                "query",
                "provider",
                query_attrs={
                    "placeholder": "Input your search terms",
                    "autocomplete": "off",
                    "maxlength": 200,
                },
                provider_attrs={
                    "hx-post": reverse("search_info"),
                    "hx-target": "#search-panel",
                    "@change": "providerChanged",
                    "style": "max-width: 200px",
                    "aria-label": "Select search provider",
                },
            ),
        )

    def create_filters_layout(self) -> Layout:
        return Layout(
            Field("modalities", css_class="form-select-sm"),
            Field("study_date_from", css_class="form-control-sm"),
            Field("study_date_till", css_class="form-control-sm"),
            Field("study_description", css_class="form-control-sm"),
            Field("patient_sex", css_class="form-select-sm"),
            RangeSlider("Age range", "age_from", "age_till", group_class="input-group-sm"),
            Div(
                Button(
                    "reset_filters", "Reset filters", type="button", css_class="btn btn-secondary"
                ),
                css_class="d-flex justify-content-center",
            ),
        )

    def clean_provider(self) -> str:
        if self["provider"].html_name not in self.data:
            return self.fields["provider"].initial
        return self.cleaned_data["provider"]

    def clean_age_from(self) -> int:
        age_from = self.cleaned_data["age_from"]
        if age_from % AGE_STEP != 0:
            raise forms.ValidationError(f"Age from must be a multiple of {AGE_STEP}")
        return age_from

    def clean_age_till(self) -> int:
        age_till = self.cleaned_data["age_till"]
        if age_till % AGE_STEP != 0:
            raise forms.ValidationError(f"Age till must be a multiple of {AGE_STEP}")
        return age_till

    def clean(self) -> dict[str, Any]:
        self.age_from = self.cleaned_data["age_from"]
        self.age_till = self.cleaned_data["age_till"]

        if self.age_from >= self.age_till:
            raise forms.ValidationError("Age from must be less than age till")

        return super().clean()
