import math
from typing import Any

from crispy_forms.helper import FormHelper, Layout
from crispy_forms.layout import Button, Div, Field
from django import forms
from django.db import models

from radis.core.form_fields import (
    create_age_range_fields,
    create_language_field,
    create_modality_field,
)
from radis.core.layouts import RangeSlider
from radis.labels.models import Label

from .layouts import QueryInput
from .site import SearchFilters


def semantic_wait_notice(seconds: float) -> str:
    """The page's wording while the embedding gate is closed after a 429."""
    whole = max(1, math.ceil(seconds))
    if whole < 60:
        wait = f"about {whole} second{'s' if whole != 1 else ''}"
    else:
        minutes = math.ceil(whole / 60)
        wait = f"about {minutes} minute{'s' if minutes != 1 else ''}"
    return f"Semantic search is rate limited right now. Try again in {wait}."


class SearchMode(models.TextChoices):
    # The empty value is full-text, so the default never appears in the URL.
    FULLTEXT = "", "Full-text"
    SEMANTIC = "semantic", "Semantic"


class ModeSelect(forms.Select):
    """The mode select, with the options in ``disabled_values`` rendered disabled."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.disabled_values: set[str] = set()

    def create_option(self, name, value, label, selected, index, subindex=None, attrs=None):
        option = super().create_option(
            name, value, label, selected, index, subindex=subindex, attrs=attrs
        )
        if value in self.disabled_values:
            option["attrs"]["disabled"] = True
        return option


class SearchForm(forms.Form):
    # Query fields
    query = forms.CharField(required=False, label=False, max_length=200)  # type: ignore
    mode = forms.ChoiceField(required=False, choices=SearchMode, label=False, widget=ModeSelect)  # type: ignore
    description = forms.CharField(required=False, label=False, max_length=1000)  # type: ignore
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

    def __init__(self, *args, semantic: bool = False, semantic_wait: float = 0.0, **kwargs):
        """``semantic`` is the registered provider's capability: without it the mode select
        and the description do not exist on the page. ``semantic_wait`` is how long this
        process may not embed a query (the embedding gate's pause after a 429): for that long
        the row says when to retry and the Semantic option is disabled, unless the page is
        already in semantic mode."""
        super().__init__(*args, **kwargs)

        self.semantic_notice: str | None = None
        if not semantic:
            for name in ("mode", "description"):
                del self.fields[name]
        else:
            if semantic_wait > 0:
                self.semantic_notice = semantic_wait_notice(semantic_wait)
                # A disabled option is left out of a submission, so a page already in
                # semantic mode keeps its option: resubmitting it reaches the gate, which
                # refuses it with the same notice, instead of quietly turning into an
                # empty full-text search.
                if self.data.get("mode") != SearchMode.SEMANTIC:
                    mode_widget = self.fields["mode"].widget
                    assert isinstance(mode_widget, ModeSelect)
                    mode_widget.disabled_values = {SearchMode.SEMANTIC}

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
                notice=self.semantic_notice,
                query_attrs={"placeholder": "Input your search terms", "autocomplete": "off"},
            ),
        )

    def create_filters_layout(self) -> Layout:
        label_field = (
            [Field("labels", css_class="form-select-sm")]
            if self.fields["labels"].choices  # type: ignore
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
        return self.cleaned_data.get("mode") == SearchMode.SEMANTIC

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

    def clean(self) -> dict[str, Any] | None:
        # The page disables the inactive mode's fields, so a value for them can only come from a
        # hand-made URL or a page without JavaScript; it must neither block the search nor reach
        # the view.
        inactive = {"query": ""} if self.semantic_mode() else {"description": ""}
        for name, empty in inactive.items():
            if name in self.fields:
                self.errors.pop(name, None)
                self.cleaned_data[name] = empty

        # A field that failed its own validation is absent from cleaned_data.
        age_from = self.cleaned_data.get("age_from")
        age_till = self.cleaned_data.get("age_till")

        if age_from is not None and age_till is not None and age_from >= age_till:
            raise forms.ValidationError("Age from must be less than age till")

        return super().clean()
