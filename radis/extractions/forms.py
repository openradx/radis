import json
from typing import Any, cast

from adit_radis_shared.accounts.models import User
from crispy_forms.helper import FormHelper
from crispy_forms.layout import HTML, Column, Div, Layout, Row, Submit
from django import forms
from django.conf import settings
from django.db.models import QuerySet

from radis.core.constants import LANGUAGE_LABELS
from radis.core.layouts import RangeSlider
from radis.reports.models import Language, Modality
from radis.search.forms import AGE_STEP, MAX_AGE, MIN_AGE
from radis.search.site import Search, SearchFilters
from radis.search.utils.query_parser import QueryParser

from .constants import MAX_SELECTION_OPTIONS
from .models import ExtractionJob, OutputField, OutputType
from .site import extraction_retrieval_provider


class SearchForm(forms.ModelForm):
    class Meta:
        model = ExtractionJob
        fields = [
            "title",
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
            "title": "Title of the extraction job",
            "query": "A query to find reports for further analysis",
        }

    def __init__(self, *args, **kwargs):
        self.user: User = kwargs.pop("user")

        super().__init__(*args, **kwargs)

        self.fields["language"].choices = [  # type: ignore
            (language.pk, LANGUAGE_LABELS[language.code])
            for language in Language.objects.order_by("code")
        ]
        self.fields["modalities"].choices = [  # type: ignore
            (modality.pk, modality.code)
            for modality in Modality.objects.filter(filterable=True).order_by("code")
        ]
        self.fields["modalities"].widget.attrs["size"] = 6
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
        self.helper.disable_csrf = True
        self.helper.layout = self.build_layout()

    def build_layout(self):
        return Layout(
            Row(
                Column(
                    "title",
                    "query",
                    Submit("next", "Next Step (Output Fields)", css_class="btn-primary"),
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

    def clean_query(self) -> str:
        query = self.cleaned_data["query"]
        query_node, _ = QueryParser().parse(query)
        if query_node is None:
            raise forms.ValidationError("Invalid empty query")
        return query

    def clean(self) -> dict[str, Any] | None:
        cleaned_data = super().clean()
        assert cleaned_data

        active_group = self.user.active_group

        language = cast(Language, cleaned_data["language"])
        modalities = cast(QuerySet[Modality], cleaned_data["modalities"])

        query_node, fixes = QueryParser().parse(cleaned_data["query"])
        assert query_node

        if len(fixes) > 0:
            cleaned_data["fixed_query"] = QueryParser.unparse(query_node)

        search = Search(
            query=query_node,
            offset=0,
            limit=0,
            filters=SearchFilters(
                group=active_group.pk,
                language=language.code,
                modalities=list(modalities.values_list("code", flat=True)),
                study_date_from=cleaned_data["study_date_from"],
                study_date_till=cleaned_data["study_date_till"],
                study_description=cleaned_data["study_description"],
                patient_sex=cleaned_data["patient_sex"],
                patient_age_from=cleaned_data["age_from"],
                patient_age_till=cleaned_data["age_till"],
            ),
        )

        if extraction_retrieval_provider is None:
            raise forms.ValidationError("Extraction retrieval provider is not configured.")
        retrieval_count = extraction_retrieval_provider.count(search)
        cleaned_data["retrieval_count"] = retrieval_count

        if retrieval_count > settings.EXTRACTION_MAXIMUM_REPORTS_COUNT:
            raise forms.ValidationError(
                f"Your search returned more results ({retrieval_count}) than the extraction "
                f"pipeline can handle (max. {settings.EXTRACTION_MAXIMUM_REPORTS_COUNT}). "
                "Please refine your search."
            )

        if (
            extraction_retrieval_provider.max_results
            and retrieval_count > extraction_retrieval_provider.max_results
        ):
            raise forms.ValidationError(
                f"Your search returned more results ({retrieval_count}) than the extraction "
                "provider can handle. Please refine your search."
            )

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
        self.helper.form_tag = False  # because we want to define only the inner layout
        self.helper.disable_csrf = True

        # Build the layout for selection options and array toggle button using crispy.
        self.helper.layout = Layout(
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
        )

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


OutputFieldFormSet = forms.inlineformset_factory(
    ExtractionJob,
    OutputField,
    form=OutputFieldForm,
    extra=0,
    min_num=1,
    max_num=5,
    validate_min=True,
    validate_max=True,
    can_delete=False,
)


class SummaryForm(forms.Form):
    send_finished_mail = forms.BooleanField(
        label="Notify me via Email when job is finished",
        required=False,
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.helper = FormHelper()
        self.helper.form_tag = False
        self.helper.disable_csrf = True
