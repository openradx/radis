from crispy_forms.helper import FormHelper
from crispy_forms.layout import Column, Layout, Row
from django import forms

from radis.core.constants import LANGUAGE_LABELS
from radis.rag.site import retrieval_providers
from radis.reports.models import Language, Modality
from radis.search.forms import AGE_STEP, MAX_AGE, MIN_AGE
from radis.search.layouts import RangeSlider

from .models import Subscription


class SearchForm(forms.ModelForm):
    class Meta:
        model = Subscription
        fields = [
            "name",
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
            "patient_id",
        ]
        help_texts = {
            "name": "Name of the Subscription",
            "provider": "The search provider to use for the database query",
            "query": "A query to find reports",
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.fields["provider"].widget = forms.Select(
            choices=sorted(
                [(provider.name, provider.name) for provider in retrieval_providers.values()]
            )
        )
        self.fields["language"].choices = [
            (language.pk, LANGUAGE_LABELS[language.code])
            for language in Language.objects.order_by("code")
        ]
        self.fields["modalities"].choices = [
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
        self.fields["patient_id"] = forms.CharField(
            required=False,
        )

        self.helper = FormHelper()
        self.helper.form_tag = False
        self.helper.layout = self.build_layout()

    def build_layout(self):
        return Layout(
            Row(
                Column("name", css_class="form-group col-md-6 mb-0"),
                Column("provider", css_class="form-group col-md-6 mb-0"),
                css_class="form-row",
            ),
            Row(
                Column("query", css_class="form-group col-md-12 mb-0"),
                css_class="form-row",
            ),
            Row(
                Column("language", css_class="form-group col-md-6 mb-0"),
                Column("modalities", css_class="form-group col-md-6 mb-0"),
                css_class="form-row",
                id="filters",
            ),
            Row(
                Column("study_date_from", css_class="form-group col-md-6 mb-0"),
                Column("study_date_till", css_class="form-group col-md-6 mb-0"),
                css_class="form-row",
                id="filters",
            ),
            Row(
                Column("study_description", css_class="form-group col-md-12 mb-0"),
                css_class="form-row",
                id="filters",
            ),
            Row(
                Column("patient_sex", css_class="form-group col-md-4 mb-0"),
                Column("patient_id", css_class="form-group col-md-4 mb-0"),
                Column(
                    RangeSlider("Patient age", "age_from", "age_till"),
                    css_class="form-group col-md-4 mb-0",
                ),
                css_class="form-row",
                id="filters",
            ),
        )
