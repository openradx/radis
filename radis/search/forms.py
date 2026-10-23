from typing import Any

from crispy_forms.helper import FormHelper, Layout
from crispy_forms.layout import Button, Div, Field
from django import forms
from django.conf import settings

from radis.core.constants import AGE_STEP
from radis.core.form_fields import (
    create_age_range_fields,
    create_language_field,
    create_modality_field,
)
from radis.core.layouts import RangeSlider
from radis.labels.models import Label

from .layouts import QueryInput
from .site import SearchFilters

QUERY_MAX_LENGTH = 200
DESCRIPTION_MAX_LENGTH = 1000
MODE_SEMANTIC = "semantic"
MODE_CHOICES = [("", "Full-text"), (MODE_SEMANTIC, "Semantic")]


class SearchForm(forms.Form):
    # Query fields
    query = forms.CharField(required=False, label=False, max_length=QUERY_MAX_LENGTH)  # type: ignore
    # The empty value is full-text, so the default never appears in the URL.
    mode = forms.ChoiceField(required=False, choices=MODE_CHOICES, label=False)  # type: ignore
    description = forms.CharField(required=False, label=False, max_length=DESCRIPTION_MAX_LENGTH)  # type: ignore
    # Semantic mode only: overrides SEARCH_SEMANTIC_MIN_SIMILARITY for one search.
    min_similarity = forms.FloatField(
        required=False,
        min_value=0.0,
        max_value=1.0,
        label="Minimum similarity",
        widget=forms.NumberInput(attrs={"step": "0.001"}),
    )
    # Filter fields - language, modalities, labels, and age fields created in __init__
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

    def __init__(self, *args, semantic: bool = False, **kwargs):
        """``semantic`` is the registered provider's capability: without it the
        mode select, the description and the cutoff do not exist on the page."""
        super().__init__(*args, **kwargs)

        if not semantic:
            for name in ("mode", "description", "min_similarity"):
                del self.fields[name]
        else:
            default_cutoff = settings.SEARCH_SEMANTIC_MIN_SIMILARITY
            if default_cutoff is not None:
                self.fields["min_similarity"].widget.attrs["placeholder"] = (
                    f"default {default_cutoff:.3f}"
                )

        # Create fields using factory functions (use codes, not PKs)
        self.fields["language"] = create_language_field(use_pk=False, empty_label="All")
        self.fields["modalities"] = create_modality_field(use_pk=False)
        age_from, age_till = create_age_range_fields()
        self.fields["age_from"] = age_from
        self.fields["age_till"] = age_till

        active_labels = Label.objects.filter(active=True).order_by("name")
        self.fields["labels"] = forms.MultipleChoiceField(
            required=False, choices=[(label.name, label.name) for label in active_labels]
        )
        self.fields["labels"].widget.attrs["size"] = 6

        self.query_helper = FormHelper()
        self.query_helper.template = "search/form_elements/form_part.html"  # type: ignore
        self.query_helper.form_tag = False
        self.query_helper.disable_csrf = True
        self.query_helper.layout = self.create_query_layout()

        self.filters_helper = FormHelper()
        self.filters_helper.template = "search/form_elements/form_part.html"  # type: ignore
        self.filters_helper.form_tag = False
        self.filters_helper.disable_csrf = True
        # The page lists every field error in one alert; inline errors would repeat them.
        self.filters_helper.form_show_errors = False
        self.filters_helper.layout = self.create_filters_layout()

    def create_query_layout(self) -> Layout:
        semantic = "mode" in self.fields
        return Layout(
            QueryInput(
                "query",
                mode_field="mode" if semantic else None,
                description_field="description" if semantic else None,
                query_attrs={
                    "placeholder": "Input your search terms",
                    "autocomplete": "off",
                    "maxlength": QUERY_MAX_LENGTH,
                },
            ),
        )

    def create_filters_layout(self) -> Layout:
        label_field = (
            [Field("labels", css_class="form-select-sm")]
            if self.fields["labels"].choices  # type: ignore
            else []
        )
        cutoff_field = (
            [
                Div(
                    Field(
                        "min_similarity",
                        css_class="form-control-sm",
                        **{":disabled": "mode !== 'semantic'"},
                    ),
                    **{"x-show": "mode === 'semantic'", "x-cloak": True},
                )
            ]
            if "min_similarity" in self.fields
            else []
        )
        return Layout(
            Field("language", css_class="form-select-sm"),
            Field("modalities", css_class="form-select-sm"),
            *label_field,
            Field("study_date_from", css_class="form-control-sm"),
            Field("study_date_till", css_class="form-control-sm"),
            Field("study_description", css_class="form-control-sm"),
            Field("patient_sex", css_class="form-select-sm"),
            RangeSlider("Age range", "age_from", "age_till", group_class="input-group-sm"),
            *cutoff_field,
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

    def semantic_mode(self) -> bool:
        return self.cleaned_data.get("mode") == MODE_SEMANTIC

    def to_filters(self, group_id: int) -> SearchFilters:
        """The structured filters of a validated form, scoped to ``group_id``."""
        data = self.cleaned_data
        return SearchFilters(
            group=group_id,
            language=data["language"],
            modalities=data["modalities"],
            study_date_from=data["study_date_from"],
            study_date_till=data["study_date_till"],
            study_description=data["study_description"],
            patient_sex=data["patient_sex"],
            patient_age_from=data["age_from"],
            patient_age_till=data["age_till"],
            labels=data["labels"],
        )

    def clean_description(self) -> str:
        return " ".join(self.cleaned_data["description"].split())

    def clean_age_from(self) -> int | None:
        age_from = self.cleaned_data["age_from"]
        if age_from is not None and age_from % AGE_STEP != 0:
            raise forms.ValidationError(f"Age from must be a multiple of {AGE_STEP}")
        return age_from

    def clean_age_till(self) -> int | None:
        age_till = self.cleaned_data["age_till"]
        if age_till is not None and age_till % AGE_STEP != 0:
            raise forms.ValidationError(f"Age till must be a multiple of {AGE_STEP}")
        return age_till

    def clean(self) -> dict[str, Any] | None:
        # The page disables the inactive mode's fields, so a value for them can only come from a
        # hand-made URL or a page without JavaScript; it must neither block the search nor reach
        # the view.
        inactive = (
            {"query": ""} if self.semantic_mode() else {"description": "", "min_similarity": None}
        )
        for name, empty in inactive.items():
            if name in self.fields:
                self.errors.pop(name, None)
                self.cleaned_data[name] = empty

        # An age its own clean method rejected is absent from cleaned_data.
        age_from = self.cleaned_data.get("age_from")
        age_till = self.cleaned_data.get("age_till")

        if age_from is not None and age_till is not None and age_from >= age_till:
            raise forms.ValidationError("Age from must be less than age till")

        return super().clean()
