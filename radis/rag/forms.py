from typing import Any

from crispy_forms.helper import FormHelper
from crispy_forms.layout import HTML, Column, Div, Layout, Row, Submit
from django import forms

from adit_radis_shared.accounts.models import User
from radis.reports.models import Modality
from radis.search.forms import AGE_STEP, MAX_AGE, MIN_AGE
from radis.search.layouts import RangeSlider

from .models import Question, RagJob


class SearchForm(forms.ModelForm):
    class Meta:
        model = RagJob
        fields = [
            "title",
            "provider",
            "query",
            "modalities",
            "study_date_from",
            "study_date_till",
            "study_description",
            "patient_sex",
            "age_from",
            "age_till",
        ]
        help_texts = {
            "title": "Title of the RAG job",
            "provider": "The search provider to use for the database query",
            "query": "A query to find reports for further analysis",
        }

    def __init__(self, *args, **kwargs):
        self.user: User = kwargs.pop("user")

        super().__init__(*args, **kwargs)

        self.fields["query"].widget = forms.Textarea(attrs={"rows": 2})
        self.fields["modalities"] = forms.MultipleChoiceField(required=False)
        modalities = Modality.objects.filter(filterable=True).values_list("code", flat=True)
        modality_choices = [(m, m) for m in modalities]
        self.fields["modalities"].choices = modality_choices
        self.fields["study_date_from"].widget = forms.DateInput(attrs={"type": "date"})
        self.fields["study_date_till"].widget = forms.DateInput(attrs={"type": "date"})
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

        self.helper = FormHelper()
        self.helper.form_tag = False
        self.helper.layout = self.build_layout()

    def build_layout(self):
        layout = Layout(
            Row(
                Column(
                    "title",
                    "provider",
                    "query",
                    "question",
                    Submit("next", "Next Step (Questions)", css_class="btn-primary"),
                ),
                Column(
                    "modalities",
                    "study_date_from",
                    "study_date_till",
                    "study_description",
                    "patient_sex",
                    RangeSlider("Age range", "age_from", "age_till"),
                    css_class="col-3",
                ),
            )
        )
        return layout

    def clean(self) -> dict[str, Any]:
        if not self.fields["provider"].choices:
            raise forms.ValidationError(
                "Setup of RADIS is incomplete. No retrieval providers are registered."
            )
        return super().clean()


class QuestionForm(forms.ModelForm):
    class Meta:
        model = Question
        fields = [
            "question",
            "accepted_answer",
        ]


delete_button = """
    {% load bootstrap_icon from core_extras %}
    <button type="button"
            name="delete-question"
            value="delete-question"
            class="btn btn-sm btn-outline-danger d-none position-absolute top-0 end-0"
            :class="{'d-none': questionsCount === 1}"
            @click="deleteQuestion($el)"
            aria-label="Delete question">
        {% bootstrap_icon 'trash' %}
    </button>
"""


class QuestionFormSetHelper(FormHelper):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.form_tag = False
        self.disable_csrf = True
        self.layout = Layout(
            Div(
                Div(
                    "question",
                    "accepted_answer",
                    css_class="card-body",
                ),
                HTML(delete_button),
                css_class="card mb-3",
            )
        )


QuestionFormSet = forms.inlineformset_factory(
    RagJob,
    Question,
    form=QuestionForm,
    extra=0,
    min_num=1,
    max_num=3,
    validate_max=True,
    can_delete=False,
)
