from adit_radis_shared.accounts.models import User
from crispy_forms.helper import FormHelper
from crispy_forms.layout import HTML, Column, Div, Layout, Row, Submit
from django import forms

from radis.core.constants import LANGUAGE_LABELS
from radis.reports.models import Language, Modality
from radis.search.forms import AGE_STEP, MAX_AGE, MIN_AGE
from radis.search.layouts import RangeSlider
from radis.search.utils.query_parser import QueryParser

from .models import Question, RagJob
from .site import retrieval_providers


class SearchForm(forms.ModelForm):
    class Meta:
        model = RagJob
        fields = [
            "title",
            "provider",
            "query",
            "language",
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

        self.fields["provider"].widget = forms.Select(
            choices=[(provider.name, provider.name) for provider in retrieval_providers.values()]
        )
        self.fields["language"].choices = [  # type: ignore
            (language.pk, LANGUAGE_LABELS[language.code])
            for language in Language.objects.order_by("code")
        ]
        self.fields["modalities"].choices = [  # type: ignore
            (modality.pk, modality.code)
            for modality in Modality.objects.filter(filterable=True).order_by("code")
        ]
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
                    Submit("next", "Next Step (Questions)", css_class="btn-primary"),
                ),
                Column(
                    "language",
                    "modalities",
                    "study_date_from",
                    "study_date_till",
                    "study_description",
                    "patient_sex",
                    RangeSlider("Age range", "age_from", "age_till"),
                    css_class="col-3",
                    id="filters",
                ),
            )
        )
        return layout

    def clean_provider(self) -> str:
        provider = self.cleaned_data["provider"]
        if not provider:
            raise forms.ValidationError(
                "Setup of RADIS is incomplete. No retrieval providers are registered."
            )
        return provider

    def clean_query(self) -> str:
        query = self.cleaned_data["query"]
        query_node, _ = QueryParser().parse(query)
        if query_node is None:
            raise forms.ValidationError("Invalid empty query")
        return query


class QuestionForm(forms.ModelForm):
    class Meta:
        model = Question
        fields = [
            "question",
            "accepted_answer",
        ]


delete_button = """
    {% load bootstrap_icon from common_extras %}
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
