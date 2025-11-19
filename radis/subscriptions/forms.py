from typing import Any

from crispy_forms.helper import FormHelper
from crispy_forms.layout import Column, Div, Field, Layout, Row
from django import forms

from radis.core.constants import LANGUAGE_LABELS
from radis.core.layouts import Formset, RangeSlider
from radis.extractions.models import OutputField
from radis.reports.models import Language, Modality
from radis.search.forms import AGE_STEP, MAX_AGE, MIN_AGE

from .models import FilterQuestion, Subscription
from .site import subscription_retrieval_providers


class SubscriptionForm(forms.ModelForm):
    class Meta:
        model = Subscription
        fields = [
            "name",
            "provider",
            "query",
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
            "provider": "The search provider to use for the database query",
            "query": "A query to filter reports",
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.fields["provider"].widget = forms.Select(
            choices=sorted(
                [
                    (provider.name, provider.name)
                    for provider in subscription_retrieval_providers.values()
                ]
            )
        )
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
                    "provider",
                    "query",
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

    def clean_question(self):
        question = self.cleaned_data["question"]
        if len(question) > 300:  # already enforced by model
            raise forms.ValidationError("Question too long")
        return question

    def clean(self) -> dict[str, Any]:
        cleaned_data = super().clean()
        assert cleaned_data

        question = cleaned_data.get("question")
        expected_answer = cleaned_data.get("expected_answer")

        if (question and not expected_answer) or (expected_answer and not question):
            raise forms.ValidationError("You must provide both a question and an expected answer.")

        return cleaned_data


class OutputFieldForm(forms.ModelForm):
    class Meta:
        model = OutputField
        fields = ["name", "description", "output_type"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.fields["name"].required = True
        self.fields["description"].required = True

        self.helper = FormHelper()
        self.helper.form_tag = False
        self.helper.disable_csrf = True

        fields = [
            Field("id", type="hidden"),
            Row(
                Column("name", css_class="col-6"),
                Column("output_type", css_class="col-4"),
            ),
            "description",
        ]

        if "DELETE" in self.fields:
            fields.insert(1, Field("DELETE", type="hidden"))

        self.helper.layout = Layout(Div(*fields))


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
