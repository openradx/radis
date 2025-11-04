from __future__ import annotations

from typing import Any

from django import forms
from django.utils import timezone

LANGUAGE_CHOICES = [
    ("en", "English"),
    ("de", "German"),
]


class GenerateReportForm(forms.Form):
    language = forms.ChoiceField(
        required=True,
        choices=LANGUAGE_CHOICES,
        help_text="Limit generation to English or German.",
    )
    patient_id = forms.CharField(required=False, max_length=64)
    patient_birth_date = forms.DateField(
        required=False,
        widget=forms.DateInput(attrs={"type": "date"}),
    )
    patient_sex = forms.ChoiceField(
        required=False,
        choices=[("", "---------"), ("M", "Male"), ("F", "Female"), ("O", "Other")],
    )
    study_description = forms.CharField(required=False, max_length=64)
    study_datetime = forms.DateTimeField(
        required=False,
        widget=forms.DateTimeInput(attrs={"type": "datetime-local"}),
        input_formats=["%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M"],
    )
    study_instance_uid = forms.CharField(required=False, max_length=64)
    accession_number = forms.CharField(required=False, max_length=32)
    modalities = forms.CharField(
        required=False,
        help_text="Comma separated modality codes (e.g. CT,MR).",
    )
    instruction = forms.CharField(
        required=False,
        widget=forms.Textarea(attrs={"rows": 4}),
        help_text="Describe the report you want the LLM to generate.",
    )
    count = forms.IntegerField(
        required=False,
        min_value=1,
        max_value=10,
        initial=1,
        help_text="How many reports to generate in one go (max 10).",
    )

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            widget = field.widget
            base_class = "form-select" if isinstance(widget, forms.Select) else "form-control"
            existing_class = widget.attrs.get("class", "")
            widget.attrs["class"] = f"{existing_class} {base_class}".strip()

    def clean_language(self) -> str:
        value: str = self.cleaned_data.get("language", "en")
        return value

    def clean_modalities(self) -> list[str] | None:
        value: str = self.cleaned_data.get("modalities", "")
        if not value:
            return None
        return [item.strip().upper() for item in value.split(",") if item.strip()]

    def clean_patient_sex(self) -> str | None:
        value: str = self.cleaned_data.get("patient_sex", "")
        return value or None

    def clean_study_datetime(self):
        value = self.cleaned_data.get("study_datetime")
        if value is None:
            return None
        if timezone.is_naive(value):
            return timezone.make_aware(value)
        return value

    def get_report_kwargs(self) -> dict[str, Any]:
        cleaned = self.cleaned_data
        kwargs: dict[str, Any] = {}
        for field in (
            "patient_id",
            "patient_birth_date",
            "patient_sex",
            "study_description",
            "study_datetime",
            "study_instance_uid",
            "accession_number",
        ):
            value = cleaned.get(field)
            if value:
                kwargs[field] = value
        if cleaned.get("modalities"):
            kwargs["modalities"] = cleaned["modalities"]
        return kwargs
