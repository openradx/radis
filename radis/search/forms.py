from crispy_forms.helper import FormHelper, Layout
from crispy_forms.layout import HTML, Button, Div, Field
from django import forms
from django.urls import reverse

from radis.search.layout import DateRange, QueryInput, RangeSlider

from .site import search_handlers

SEARCH_HANDLER_CHOICES = sorted(
    [(handler.name, handler.name) for handler in search_handlers.values()]
)


class SearchForm(forms.Form):
    query = forms.CharField(required=False, label=False)
    algorithm = forms.ChoiceField(
        required=False,
        choices=SEARCH_HANDLER_CHOICES,
        label=False,
    )
    study_date_from = forms.DateField(
        required=False, widget=forms.DateInput(attrs={"type": "date"})
    )
    study_date_till = forms.DateField(
        required=False, widget=forms.DateInput(attrs={"type": "date"})
    )
    study_description = forms.CharField(required=False)
    modalities = forms.MultipleChoiceField(required=False, choices=[])
    patient_sex = forms.ChoiceField(
        required=False, choices=[("", "All"), ("M", "Male"), ("F", "Female")]
    )
    age_from = forms.IntegerField(
        required=False,
        widget=forms.NumberInput(
            attrs={
                "type": "range",
                "min": 0,
                "max": 120,
                "value": 0,
                "step": 10,
            }
        ),
    )
    age_till = forms.IntegerField(
        required=False,
        widget=forms.NumberInput(
            attrs={
                "type": "range",
                "min": 0,
                "max": 120,
                "value": 120,
                "step": 10,
            }
        ),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.helper = FormHelper()
        self.helper.form_method = "GET"
        self.helper.attrs = {"x-data": "SearchForm()"}

        layout = Layout(
            Div(
                QueryInput(
                    "query",
                    "algorithm",
                    query_attrs={
                        "placeholder": "Input your search terms",
                        "autocomplete": "off",
                        "maxlength": 200,
                    },
                    algorithm_attrs={
                        "hx-post": reverse("search_info"),
                        "hx-target": "#search-panel",
                        "@change": "algorithmChanged",
                        "style": "max-width: 200px",
                        "aria-label": "Select search algorithm",
                    },
                ),
                HTML(
                    """
                    <div class="d-flex justify-content-end mt-1">
                        <a href="">No Filters Active [+]</a>
                    </div>
                    """
                ),
                Div(
                    Field(
                        "modalities",
                        css_class="form-select-sm",
                    ),
                    DateRange(
                        "Date range",
                        "study_date_from",
                        "study_date_till",
                        css_class="form-control-sm",
                        group_class="input-group-sm",
                    ),
                    Field(
                        "study_description",
                        css_class="form-control-sm",
                        style="min-width: 250px;",
                    ),
                    Field(
                        "patient_sex",
                        css_class="form-select-sm",
                    ),
                    RangeSlider("Age range", "age_from", "age_till", group_class="input-group-sm"),
                    Div(
                        Button(
                            "reset_filters",
                            "Reset filters",
                            type="button",
                            css_class="btn btn-secondary",
                        ),
                    ),
                    css_class="d-flex flex-wrap gap-3",
                ),
                css_class="d-flex flex-column",
            ),
        )
        self.helper.layout = layout
