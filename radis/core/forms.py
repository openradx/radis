from typing import Any, Literal, NamedTuple, cast

from crispy_forms.bootstrap import FieldWithButtons
from crispy_forms.helper import FormHelper
from crispy_forms.layout import Div, Field, Hidden, Layout, Submit
from django import forms
from django.core.files.uploadedfile import UploadedFile
from django.http.request import QueryDict
from django.utils.functional import classproperty  # type: ignore


class BroadcastForm(forms.Form):
    subject = forms.CharField(label="Subject", max_length=200)
    message = forms.CharField(label="Message", max_length=10000, widget=forms.Textarea)


class PageSizeSelectForm(forms.Form):
    per_page = forms.ChoiceField(required=False, label="Items per page")

    def __init__(self, data, pages_sizes, *args, **kwargs):
        super().__init__(data, *args, **kwargs)

        choices = [(size, size) for size in pages_sizes]
        per_page_field = cast(forms.ChoiceField, self.fields["per_page"])
        per_page_field.choices = choices

        # For simplicity we reuse the FilterSetFormHelper here (normally used for filters)
        form_helper = FilterSetFormHelper(data)
        form_helper.add_filter_field("per_page", "select", button_label="Set")
        form_helper.build_filter_set_layout()
        self.helper = form_helper


class FilterSetFormHelper(FormHelper):
    """All filters of one model are rendered in one form."""

    class FilterField(NamedTuple):
        field_name: str
        field_type: Literal["select", "text"]
        button_label: str = "Set"

    def __init__(
        self,
        params: QueryDict | dict,
        **kwargs,
    ):
        super().__init__(**kwargs)

        self.form_method = "get"
        self.disable_csrf = True

        self.params = params
        self.layout = Layout()
        self.filter_fields: list[FilterSetFormHelper.FilterField] = []

    def add_filter_field(
        self, field_name: str, field_type: Literal["select", "text"], button_label: str = "Set"
    ):
        self.filter_fields.append(
            FilterSetFormHelper.FilterField(field_name, field_type, button_label)
        )

    def build_filter_set_layout(self):
        field_names = []

        visible_fields = Div(css_class="d-flex gap-3")
        self.layout.append(visible_fields)

        for filter_field in self.filter_fields:
            field_names.append(filter_field.field_name)

            # TODO: FieldWithButtons do not work correctly with select widget, we
            # have to add the CSS class manually
            # https://github.com/django-crispy-forms/crispy-bootstrap5/issues/148
            if filter_field.field_type == "select":
                field_class = "form-select form-select-sm"
            else:
                field_class = "form-control-sm"

            visible_fields.append(
                FieldWithButtons(
                    Field(filter_field.field_name, css_class=field_class),
                    Submit(
                        "",
                        filter_field.button_label,
                        css_class="btn-secondary btn-sm",
                    ),
                    template="core/_filter_set_field.html",
                ),
            )

        hidden_fields = Div()
        self.layout.append(hidden_fields)

        for key in self.params:
            if key not in field_names:
                hidden_fields.append(Hidden(key, self.params.get(key)))


class CombinedForm:
    """Helper class that can be used with a FormWizzardView to combine multiple forms into one.

    If used with a wizard to edit already existing instances, one has to also overwrite
    `get_form` of the wizard to set the model instances manually as those are only set
    for `ModelForm` and `BaseModelFormSet`.
    """

    form_classes: dict[str, type[forms.Form] | type[forms.formsets.BaseFormSet]]

    def __init__(self, *args, **kwargs):
        self.form_instances: dict[str, forms.Form | forms.formsets.BaseFormSet] = {}
        for key in self.form_classes.keys():
            self.form_instances[key] = self.get_form_instance(key, *args, **kwargs)

    def get_form_instance(
        self, key: str, *args, **kwargs
    ) -> forms.Form | forms.formsets.BaseFormSet:
        return self.form_classes[key](*args, **kwargs)

    @property
    def data(self) -> dict[str, Any]:
        data = {}
        for form in self.form_instances.values():
            data.update(form.data)
        return data

    @property
    def files(self) -> dict[str, UploadedFile]:
        files = {}
        for form in self.form_instances.values():
            files.update(form.files)
        return files

    @classproperty
    def base_fields(self) -> dict[str, forms.Field]:
        base_fields: dict[str, forms.Field] = {}
        for form in self.form_classes.values():
            form_class: type[forms.Form]
            if issubclass(form, forms.formsets.BaseFormSet):
                form_class = form.form  # type: ignore
            else:
                form_class = form
            base_fields.update(form_class.base_fields)
        return base_fields

    def is_valid(self) -> bool:
        return all(form.is_valid() for form in self.form_instances.values())
