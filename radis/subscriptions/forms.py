from typing import Any

from crispy_forms.helper import FormHelper
from crispy_forms.layout import HTML, Column, Div, Layout, Row, Submit
from django import forms

from radis.core.constants import LANGUAGE_LABELS
from radis.rag.site import retrieval_providers
from radis.reports.models import Language, Modality
from radis.search.forms import AGE_STEP, MAX_AGE, MIN_AGE
from radis.search.layouts import RangeSlider

from .models import Subscription


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
                [(provider.name, provider.name) for provider in retrieval_providers.values()]
            )
        )
        self.fields["language"].choices = [
            (language.pk, LANGUAGE_LABELS[language.code])
            for language in Language.objects.order_by("code")
        ]
        self.fields["language"].empty_label = "All"
        self.fields["modalities"].choices = [
            (modality.pk, modality.code)
            for modality in Modality.objects.filter(filterable=True).order_by("code")
        ]
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
        if self.instance.pk is None:
            submit_btn = Submit("create", "Create subscription", css_class="btn btn-primary")
            cancel_btn = HTML(
                '<a href="{% url \'subscription_list\' %}" class="btn btn-secondary">Cancel</a>'
            )
        else:
            submit_btn = Submit("update", "Update subscription", css_class="btn btn-primary")
            cancel_btn = HTML(
                f"<a href=\"{{% url 'subscription_detail' { self.instance.pk } %}}\" "
                'class="btn btn-secondary">Cancel</a>'
            )

        buttons = Div(submit_btn, cancel_btn, css_class="d-flex gap-2")

        return Layout(
            Row(
                Column(
                    "name",
                    "provider",
                    "language",
                    "query",
                    buttons,
                ),
                Column(
                    "patient_id",
                    "modalities",
                    "study_description",
                    "patient_sex",
                    RangeSlider("Patient age", "age_from", "age_till"),
                    css_class="col-3",
                ),
            ),
        )

    def clean_provider(self) -> str:
        provider = self.cleaned_data["provider"]
        if not provider:
            raise forms.ValidationError(
                "Setup of RADIS is incomplete. No retrieval providers are registered."
            )
        return provider

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

    def clean(self) -> dict[str, Any]:
        age_from = self.cleaned_data["age_from"]
        age_till = self.cleaned_data["age_till"]

        if age_from is not None and age_till is not None and age_from >= age_till:
            raise forms.ValidationError("Age from must be less than age till")

        return super().clean()
