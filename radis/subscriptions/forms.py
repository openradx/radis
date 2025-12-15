import json
from typing import Any

from crispy_forms.helper import FormHelper
from crispy_forms.layout import HTML, Column, Div, Field, Layout, Row
from django import forms

from radis.core.constants import LANGUAGE_LABELS
from radis.core.layouts import Formset, RangeSlider
from radis.extractions.constants import MAX_SELECTION_OPTIONS
from radis.extractions.models import OutputField, OutputType
from radis.reports.models import Language, Modality
from radis.search.forms import AGE_STEP, MAX_AGE, MIN_AGE

from .models import FilterQuestion, Subscription


class SubscriptionForm(forms.ModelForm):
    class Meta:
        model = Subscription
        fields = [
            "name",
            "language",
            "modalities",
            "study_description",
            "patient_sex",
            "age_from",
            "age_till",
            "patient_id",
            "send_finished_mail",
        ]
        labels = {"patient_id": "Patient ID"}
        help_texts = {
            "name": "Name of the Subscription",
            "query": (
                "Search query to filter reports "
                "(leave empty to auto-generate from extraction fields)"
            ),
        }
        widgets = {
            "query": forms.TextInput(attrs={"placeholder": "Optional - auto-generated if empty"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.fields["language"].choices = [  # type: ignore
            (language.pk, LANGUAGE_LABELS[language.code])
            for language in Language.objects.order_by("code")
        ]
        self.fields["language"].empty_label = "All"  # type: ignore
        self.fields["modalities"].choices = [  # type: ignore
            (modality.pk, modality.code)
            for modality in Modality.objects.filter(filterable=True).order_by("code")
        ]
        self.fields["modalities"].widget.attrs["size"] = 6
        self.fields["age_from"] = forms.IntegerField(
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
        self.fields["age_till"] = forms.IntegerField(
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
        self.fields["send_finished_mail"].label = "Notify me via mail of new reports"

        self.helper = FormHelper()
        self.helper.form_tag = False
        self.helper.layout = self.build_layout()

    def build_layout(self):
        return Layout(
            Row(
                Column(
                    "name",
                    "send_finished_mail",
                    Formset(
                        "filter_formset",
                        legend="Filter Questions",
                        add_form_label="Add Filter Question",
                    ),
                    Formset(
                        "output_formset",
                        legend="Extraction Fields",
                        add_form_label="Add Extraction Field",
                    ),
                ),
                Column(
                    "patient_id",
                    "language",
                    "modalities",
                    "study_description",
                    "patient_sex",
                    RangeSlider("Age range", "age_from", "age_till"),
                    css_class="col-4",
                    id="filters",
                ),
            )
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


class FilterQuestionForm(forms.ModelForm):
    class Meta:
        model = FilterQuestion
        fields = ["question", "expected_answer"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.fields["question"].required = False
        self.fields["expected_answer"].required = False
        self.fields["expected_answer"].choices = [  # type: ignore[attr-defined]
            ("", "Select the expected answer"),
            *FilterQuestion.ExpectedAnswer.choices,
        ]
        self.fields["expected_answer"].label = "Accept when answer is"

        self.helper = FormHelper()
        self.helper.form_tag = False
        self.helper.disable_csrf = True
        fields = [Field("id", type="hidden"), "question", "expected_answer"]
        if "DELETE" in self.fields:
            fields.insert(1, Field("DELETE", type="hidden"))
        self.helper.layout = Layout(Div(*fields))

    def has_changed(self) -> bool:
        if not self.is_bound:
            return super().has_changed()

        question = (self.data.get(self.add_prefix("question")) or "").strip()
        expected_answer = self.data.get(self.add_prefix("expected_answer")) or ""

        if not question and not expected_answer:
            return False

        return super().has_changed()

    def clean(self) -> dict[str, Any]:
        cleaned_data = super().clean()
        assert cleaned_data

        question = cleaned_data.get("question")
        expected_answer = cleaned_data.get("expected_answer")

        if (question and not expected_answer) or (expected_answer and not question):
            raise forms.ValidationError("You must provide both a question and an expected answer.")

        return cleaned_data


class OutputFieldForm(forms.ModelForm):
    """Hidden field to store selection options and array flag as JSON string.
    This is done because the selection options are dynamic and the array toggle
    is an alpine component that needs to be re-rendered on every change."""

    selection_options = forms.CharField(
        required=False,
        widget=forms.HiddenInput(),
    )
    is_array = forms.CharField(
        required=False,
        widget=forms.HiddenInput(),
    )

    class Meta:
        model = OutputField
        fields = [
            "name",
            "description",
            "output_type",
            "selection_options",
            "is_array",
        ]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.fields["name"].required = True
        self.fields["description"].required = True
        self.fields["description"].widget = forms.Textarea(attrs={"rows": 3})

        # Stamp data attributes so the selection-options widget can locate these fields.
        self.fields["selection_options"].widget.attrs.update(
            {
                "data-selection-input": "true",
                "data-max-selection-options": str(MAX_SELECTION_OPTIONS),
            }
        )
        self.fields["is_array"].widget.attrs.update(
            {
                "data-array-input": "true",
            }
        )

        initial_options = self.instance.selection_options if self.instance.pk else []

        # Prepopulate the hidden fields so existing options/toggle state show up in the widget.
        self.initial["selection_options"] = json.dumps(initial_options)
        self.initial["is_array"] = "true" if self.instance.is_array else "false"

        self.helper = FormHelper()
        self.helper.form_tag = False
        self.helper.disable_csrf = True

        # Build the layout for selection options and array toggle button using crispy.
        fields = [
            Field("id", type="hidden"),
            Row(
                Column("name", css_class="col-md-7 col-12"),
                Column("output_type", css_class="col-md-4 col-10"),
                Column(
                    HTML(
                        (
                            '<button type="button" '
                            'class="btn btn-outline-secondary btn-sm array-toggle-btn '
                            'form-array-toggle" '
                            'data-array-toggle="true" '
                            'aria-pressed="false" '
                            'title="Toggle array output">[ ]</button>'
                        )
                    ),
                    css_class=(
                        "col-md-1 col-2 d-flex align-items-center "
                        "justify-content-end array-toggle-field"
                    ),
                ),
                css_class="g-3 align-items-center",
            ),
            "description",
            # Include the selection options widget partial template here.
            Div(
                HTML('{% include "extractions/_selection_options_field.html" %}'),
                css_class="selection-options-wrapper",
            ),
        ]

        if "DELETE" in self.fields:
            fields.insert(1, Field("DELETE", type="hidden"))

        self.helper.layout = Layout(Div(*fields))

    # Parse and sanitize the JSON payload (trim strings, enforce uniqueness and max count).
    # Called automatically by Django form validation.
    def clean_selection_options(self) -> list[str]:
        raw_value = self.cleaned_data.get("selection_options") or ""
        raw_value = raw_value.strip()
        if raw_value == "":
            return []

        try:
            parsed = json.loads(raw_value)
        except json.JSONDecodeError as exc:
            raise forms.ValidationError("Invalid selection data.") from exc

        if not isinstance(parsed, list):
            raise forms.ValidationError("Selection data must be a list.")

        cleaned: list[str] = []
        for item in parsed:
            if not isinstance(item, str):
                raise forms.ValidationError("Selection options must be text.")
            value = item.strip()
            if not value:
                raise forms.ValidationError("Selection options cannot be empty.")
            cleaned.append(value)

        if len(cleaned) > MAX_SELECTION_OPTIONS:
            raise forms.ValidationError(
                f"Provide at most {MAX_SELECTION_OPTIONS} selection options."
            )
        if len(set(cleaned)) != len(cleaned):
            raise forms.ValidationError("Selection options must be unique.")

        return cleaned

    # not exactly needed because we set the value ourselves, but more as a double check
    def clean_is_array(self) -> bool:
        raw_value = (self.cleaned_data.get("is_array") or "").strip().lower()
        if raw_value in {"1", "true", "on"}:
            return True
        return False

    def clean(self):
        cleaned_data = super().clean()
        if not cleaned_data:
            return cleaned_data

        output_type = cleaned_data.get("output_type")
        selection_options: list[str] = cleaned_data.get("selection_options") or []

        if output_type == OutputType.SELECTION:
            if not selection_options:
                self.add_error(
                    "selection_options",
                    "Add at least one selection to use the Selection type.",
                )
        else:
            if selection_options:
                self.add_error(
                    "selection_options",
                    "Selections are only allowed when Output Type is Selection.",
                )
                cleaned_data["selection_options"] = []

        return cleaned_data


FilterQuestionFormSet = forms.inlineformset_factory(
    Subscription,
    FilterQuestion,
    form=FilterQuestionForm,
    extra=1,
    min_num=0,
    max_num=3,
    validate_max=True,
    can_delete=False,
)

OutputFieldFormSet = forms.inlineformset_factory(
    Subscription,
    OutputField,
    form=OutputFieldForm,
    fk_name="subscription",
    extra=1,
    min_num=0,
    max_num=10,
    validate_max=True,
    can_delete=False,
)
