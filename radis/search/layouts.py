from typing import Any

from crispy_forms.layout import TemplateNameMixin
from django.forms import Form
from django.template.loader import render_to_string
from django.utils.html import conditional_escape


class QueryInput(TemplateNameMixin):
    def __init__(
        self,
        query_field: str,
        css_class: str | None = None,
        query_attrs: dict[str, Any] | None = None,
        **kwargs,
    ) -> None:
        self.query_field = query_field

        self.query_attrs: dict[str, Any] = {}

        if css_class:
            self.query_attrs["class"] = css_class

        if query_attrs:
            self.query_attrs.update(query_attrs)

        self.query_attrs.update(
            {k.replace("_", "-"): conditional_escape(v) for k, v in kwargs.items()}
        )

    def render(self, form: Form, context: dict[str, Any], *args, **kwargs) -> str:
        query_field = form[self.query_field]

        if self.query_attrs:
            query_field.field.widget.attrs.update(self.query_attrs)

        return render_to_string(
            "search/form_elements/query_input.html",
            {
                "query_field": query_field,
            },
        )
