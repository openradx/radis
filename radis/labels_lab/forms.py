from dataclasses import fields

from django import forms
from django.db.models import QuerySet

from radis.labels.models import LabelGroup

from .questions import Thresholds


def groups_with_active_labels() -> QuerySet[LabelGroup]:
    return LabelGroup.objects.filter(labels__active=True).distinct()


class ThresholdsForm(forms.Form):
    gate = forms.FloatField(min_value=0, max_value=1)
    possible = forms.FloatField(min_value=0, max_value=1)
    likely = forms.FloatField(min_value=0, max_value=1)
    present = forms.FloatField(min_value=0, max_value=1)

    def clean(self):
        cleaned_data = super().clean() or {}
        # A cut that failed its own validation is missing here and reported by its field.
        cuts = [
            cleaned_data[name] for name in ("possible", "likely", "present") if name in cleaned_data
        ]
        if cuts != sorted(cuts):
            raise forms.ValidationError(
                "The P(present) thresholds must not fall from POSSIBLE over LIKELY to PRESENT."
            )
        return cleaned_data

    def thresholds(self) -> Thresholds:
        return Thresholds(**{f.name: self.cleaned_data[f.name] for f in fields(Thresholds)})


class LabForm(ThresholdsForm):
    # Not stripped: the models are sent the text exactly as it stands in the text box.
    text = forms.CharField(strip=False)
    groups = forms.ModelMultipleChoiceField(queryset=groups_with_active_labels())
    model = forms.CharField()
    run_baseline = forms.BooleanField(required=False)
    extras = forms.BooleanField(required=False)

    def clean_text(self) -> str:
        text = self.cleaned_data["text"]
        if not text.strip():
            raise forms.ValidationError("The report text is empty.")
        return text
