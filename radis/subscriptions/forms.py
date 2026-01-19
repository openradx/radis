from typing import Any

from crispy_forms.helper import FormHelper
from crispy_forms.layout import Column, Div, Field, Layout, Row
from django import forms

from radis.core.constants import LANGUAGE_LABELS
from radis.core.layouts import Formset, RangeSlider
from radis.reports.models import Language, Modality
from radis.search.forms import AGE_STEP, MAX_AGE, MIN_AGE

from .models import Question, Subscription


class SubscriptionForm(forms.ModelForm):
    class Meta:
        model = Subscription
        fields = [
            "name",
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
            "query": "A query to filter reports",
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.fields["language"].choices = [  # type: ignore
            (language.pk, LANGUAGE_LABELS.get(language.code, language.code))
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
                    "query",
                    "send_finished_mail",
                    Formset("formset", legend="Questions", add_form_label="Add Question"),
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


class QuestionForm(forms.ModelForm):
    class Meta:
        model = Question
        fields = ["question"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.fields["question"].required = False

        self.helper = FormHelper()
        self.helper.form_tag = False
        self.helper.disable_csrf = True
        self.helper.layout = Layout(
            Div(
                Field("id", type="hidden"),
                Field("DELETE", type="hidden"),
                "question",
            ),
        )


QuestionFormSet = forms.inlineformset_factory(
    Subscription,
    Question,
    form=QuestionForm,
    extra=1,
    min_num=0,
    max_num=3,
    validate_max=True,
    can_delete=False,
)
