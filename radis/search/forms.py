from typing import Any

from crispy_forms.helper import FormHelper, Layout
from crispy_forms.layout import Button, Div, Field
from django import forms

from radis.core.constants import AGE_STEP
from radis.core.form_fields import (
    create_age_range_fields,
    create_language_field,
    create_modality_field,
)
from radis.core.layouts import RangeSlider

from .layouts import QueryInput


class SearchForm(forms.Form):
    # Query fields
    query = forms.CharField(required=False, label=False)  # type: ignore
    # Filter fields - language, modalities, and age fields created in __init__
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

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        # Create fields using factory functions (use codes, not PKs)
        self.fields["language"] = create_language_field(use_pk=False)
        self.fields["modalities"] = create_modality_field(use_pk=False)
        age_from, age_till = create_age_range_fields()
        self.fields["age_from"] = age_from
        self.fields["age_till"] = age_till

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
                query_attrs={
                    "placeholder": "Input your search terms",
                    "autocomplete": "off",
                    "maxlength": 200,
                },
            ),
        )

    def create_filters_layout(self) -> Layout:
        return Layout(
            Field("language", css_class="form-select-sm"),
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
                    css_class="btn btn-sm btn-secondary",
                    **{"@click": "resetFilters"},
                ),
                css_class="d-flex justify-content-center",
            ),
        )

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

    def clean(self) -> dict[str, Any] | None:
        age_from = self.cleaned_data["age_from"]
        age_till = self.cleaned_data["age_till"]

        if age_from is not None and age_till is not None and age_from >= age_till:
            raise forms.ValidationError("Age from must be less than age till")

        return super().clean()
