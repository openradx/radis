from typing import Any

from crispy_forms.helper import FormHelper, Layout
from crispy_forms.layout import Button, Div, Field
from django import forms
from django.urls import reverse
from django.utils.functional import lazy

from radis.reports.models import Modality

from .layouts import QueryInput, RangeSlider
from .site import search_providers

MIN_AGE = 0
MAX_AGE = 120
AGE_STEP = 10


def get_search_providers():
    return [(provider.name, provider.name) for provider in search_providers.values()]


class SearchForm(forms.Form):
    # Query fields
    query = forms.CharField(required=False, label=False)
    provider = forms.ChoiceField(
        required=False,
        # TODO: in Django 5 choices can be passed a function directly
        choices=lazy(get_search_providers, tuple)(),
        label=False,
    )
    # Filter fields
    modalities = forms.MultipleChoiceField(required=False, choices=[])
    study_date_from = forms.DateField(
        required=False, widget=forms.DateInput(attrs={"type": "date"})
    )
    study_date_till = forms.DateField(
        required=False, widget=forms.DateInput(attrs={"type": "date"})
    )
    study_description = forms.CharField(required=False)
    patient_sex = forms.ChoiceField(
        required=False, choices=[("", "All"), ("M", "Male"), ("F", "Female")]
    )
    age_from = forms.IntegerField(
        required=False,
        min_value=MIN_AGE,
        max_value=MAX_AGE,
        widget=forms.NumberInput(
            attrs={
                "type": "range",
                "step": AGE_STEP,
                "value": MIN_AGE,
            }
        ),
    )
    age_till = forms.IntegerField(
        required=False,
        min_value=MIN_AGE,
        max_value=MAX_AGE,
        widget=forms.NumberInput(
            attrs={
                "type": "range",
                "step": AGE_STEP,
                "value": MAX_AGE,
            }
        ),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        search_provider_choices = self.fields["provider"].choices
        if search_provider_choices:
            self.fields["provider"].initial = search_provider_choices[0][0]

        modalities = Modality.objects.filter(filterable=True).values_list("code", flat=True)
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
                    "reset_filters",
                    "Reset filters",
                    type="button",
                    css_class="btn btn-secondary",
                    **{"@click": "resetFilters"},
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
        if age_from is not None and age_from % AGE_STEP != 0:
            raise forms.ValidationError(f"Age from must be a multiple of {AGE_STEP}")
        return age_from

    def clean_age_till(self) -> int:
        age_till = self.cleaned_data["age_till"]
        if age_till is not None and age_till % AGE_STEP != 0:
            raise forms.ValidationError(f"Age till must be a multiple of {AGE_STEP}")
        return age_till

    def clean(self) -> dict[str, Any]:
        if not self.fields["provider"].choices:
            raise forms.ValidationError(
                "Setup of RADIS is incomplete. No search providers are registered."
            )

        age_from = self.cleaned_data["age_from"]
        age_till = self.cleaned_data["age_till"]

        if age_from is not None and age_till is not None and age_from >= age_till:
            raise forms.ValidationError("Age from must be less than age till")

        return super().clean()
