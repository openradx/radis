from typing import Any

from crispy_forms.helper import FormHelper, Layout
from crispy_forms.layout import Button, Div, Field
from django import forms
from django.db.models import F, Func
from django.urls import reverse

from radis.reports.models import Report
from radis.search.layouts import QueryInput, RangeSlider

from .site import search_providers

SEARCH_PROVIDER_CHOICES = sorted(
    [(provider.name, provider.name) for provider in search_providers.values()]
)


class SearchForm(forms.Form):
    # Query fields
    query = forms.CharField(required=False, label=False)
    provider = forms.ChoiceField(
        required=False,
        choices=SEARCH_PROVIDER_CHOICES,
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
        widget=forms.NumberInput(
            attrs={
                "type": "range",
                "min": 0,
                "max": 120,
                "value": 0,
                "step": 10,
            }
        ),
    )
    age_till = forms.IntegerField(
        required=False,
        widget=forms.NumberInput(
            attrs={
                "type": "range",
                "min": 0,
                "max": 120,
                "value": 120,
                "step": 10,
            }
        ),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        if SEARCH_PROVIDER_CHOICES:
            self.fields["provider"].initial = SEARCH_PROVIDER_CHOICES[0][0]

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

    def clean(self) -> dict[str, Any]:
        if not self.fields["provider"].choices:
            raise forms.ValidationError(
                "Setup of RADIS is incomplete. No search providers are registered."
            )

        return super().clean()
