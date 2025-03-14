import django_filters
from adit_radis_shared.common.forms import SingleFilterFieldFormHelper
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

        self.form.helper = SingleFilterFieldFormHelper(self.request.GET, "text")
