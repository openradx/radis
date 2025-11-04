from typing import Any, cast

from adit_radis_shared.accounts.models import User
from crispy_forms.helper import FormHelper
from crispy_forms.layout import Column, Layout, Row, Submit
from django import forms
from django.conf import settings
from django.db.models import QuerySet

from radis.core.constants import LANGUAGE_LABELS
from radis.core.date_formats import DATE_INPUT_FORMATS
from radis.core.layouts import RangeSlider
from radis.core.widgets import DatePickerInput
from radis.reports.models import Language, Modality
from radis.search.forms import AGE_STEP, MAX_AGE, MIN_AGE
from radis.search.site import Search, SearchFilters
from radis.search.utils.query_parser import QueryParser

from .models import ExtractionJob, OutputField
from .site import extraction_retrieval_providers


class SearchForm(forms.ModelForm):
    study_date_from = forms.DateField(
        required=False,
        input_formats=DATE_INPUT_FORMATS,
        widget=DatePickerInput(),
    )
    study_date_till = forms.DateField(
        required=False,
        input_formats=DATE_INPUT_FORMATS,
        widget=DatePickerInput(),
    )
    class Meta:
        model = ExtractionJob
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
            "title": "Title of the extraction job",
            "provider": "The search provider to use for the database query",
            "query": "A query to find reports for further analysis",
        }

    def __init__(self, *args, **kwargs):
        self.user: User = kwargs.pop("user")

        super().__init__(*args, **kwargs)

        self.fields["provider"].widget = forms.Select(
            choices=[
                (provider.name, provider.name)
                for provider in extraction_retrieval_providers.values()
            ]
        )
        self.fields["language"].choices = [  # type: ignore
            (language.pk, LANGUAGE_LABELS[language.code])
            for language in Language.objects.order_by("code")
        ]
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

        self.helper = FormHelper()
        self.helper.form_tag = False
        self.helper.disable_csrf = True
        self.helper.layout = self.build_layout()

    def build_layout(self):
        return Layout(
            Row(
                Column(
                    "title",
                    "provider",
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

    def clean(self) -> dict[str, Any] | None:
        cleaned_data = super().clean()
        assert cleaned_data

        date_from = cleaned_data.get("study_date_from")
        date_till = cleaned_data.get("study_date_till")
        if date_from and date_till and date_from > date_till:
            raise forms.ValidationError("Study date from must be before study date till")

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

        provider = cleaned_data["provider"]
        assert isinstance(provider, str)
        retrieval_provider = extraction_retrieval_providers[provider]
        retrieval_count = retrieval_provider.count(search)
        cleaned_data["retrieval_count"] = retrieval_count

        if retrieval_count > settings.EXTRACTION_MAXIMUM_REPORTS_COUNT:
            raise forms.ValidationError(
                f"Your search returned more results ({retrieval_count}) then the extraction "
                f"pipeline can handle (max. {settings.EXTRACTION_MAXIMUM_REPORTS_COUNT}). "
                "Please refine your search."
            )

        if retrieval_provider.max_results and retrieval_count > retrieval_provider.max_results:
            raise forms.ValidationError(
                f"Your search returned more results ({retrieval_count}) than the extraction "
                "provider can handle. Please refine your search."
            )

        return cleaned_data


class OutputFieldForm(forms.ModelForm):
    class Meta:
        model = OutputField
        fields = [
            "name",
            "description",
            "output_type",
        ]


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
