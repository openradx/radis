from typing import Any

from crispy_forms.layout import TemplateNameMixin
from django.forms import Form
from django.template.loader import render_to_string
from django.utils.html import conditional_escape


class QueryInput(TemplateNameMixin):
    """The search row, rendered by ``form_elements/query_input.html``: one keyword input
    group, or, with a mode field, the mode select beside a keyword row and a description
    row, of which only the active mode's is shown."""

    def __init__(
        self,
        query_field: str,
        *,
        mode_field: str | None = None,
        description_field: str | None = None,
        css_class: str | None = None,
        query_attrs: dict[str, Any] | None = None,
        **kwargs,
    ) -> None:
        self.query_field = query_field
        self.mode_field = mode_field
        self.description_field = description_field

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

        description_field = form[self.description_field] if self.description_field else None
        return render_to_string(
            "search/form_elements/query_input.html",
            {
                "query_field": query_field,
                "mode_field": form[self.mode_field] if self.mode_field else None,
                "description_field": description_field,
                "description_value": description_field.value() or "" if description_field else "",
            },
        )
