import django_filters
from adit_radis_shared.common.types import with_form_helper
from crispy_forms.bootstrap import FieldWithButtons, StrictButton
from crispy_forms.helper import FormHelper, Layout
from crispy_forms.layout import Div
from django.http import HttpRequest

from .models import Note


class NoteFilter(django_filters.FilterSet):
    search = django_filters.CharFilter(field_name="text", lookup_expr="search")
    request: HttpRequest

    class Meta:
        model = Note
        fields = ("text",)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        helper = FormHelper()
        helper.form_method = "GET"
        helper.layout = Layout(
            Div(
                FieldWithButtons(
                    "search",
                    StrictButton("Search", type="submit", css_class="btn-secondary btn-sm"),
                    css_class="col-md-6",
                ),
                css_class="row",
            )
        )

        self.form.fields["search"].label = ""
        self.form.fields["search"].widget.attrs["placeholder"] = "Search notes"
        with_form_helper(self.form).helper = helper
