import django_filters
from adit_radis_shared.common.forms import FilterSetFormHelper
from django.http import HttpRequest

from .models import Note


class NoteFilter(django_filters.FilterSet):
    text = django_filters.CharFilter(lookup_expr="search", label="Search Notes")
    request: HttpRequest

    class Meta:
        model = Note
        fields = ("text",)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        form_helper = FilterSetFormHelper(self.request.GET)
        form_helper.add_filter_field("text", "text", "Search")
        form_helper.build_filter_set_layout()
        self.form.helper = form_helper
