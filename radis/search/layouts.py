from typing import Any

from crispy_forms.layout import TemplateNameMixin
from django.forms import Form
from django.template.loader import render_to_string
from django.utils.html import conditional_escape


class QueryInput(TemplateNameMixin):
    def __init__(
        self,
        query_field: str,
        provider_field: str,
        css_class: str | None = None,
        query_attrs: dict[str, Any] | None = None,
        provider_attrs: dict[str, Any] | None = None,
        **kwargs,
    ) -> None:
        self.query_field = query_field
        self.provider_field = provider_field

        self.query_attrs: dict[str, Any] = {}
        self.provider_attrs: dict[str, Any] = {}

        if css_class:
            self.query_attrs["class"] = css_class
            self.provider_attrs["class"] = css_class

        if query_attrs:
            self.query_attrs.update(query_attrs)

        if provider_attrs:
            self.provider_attrs.update(provider_attrs)

        self.query_attrs.update(
            {k.replace("_", "-"): conditional_escape(v) for k, v in kwargs.items()}
        )
        self.provider_attrs.update(
            {k.replace("_", "-"): conditional_escape(v) for k, v in kwargs.items()}
        )

    def render(self, form: Form, context: dict[str, Any], *args, **kwargs) -> str:
        query_field = form[self.query_field]
        provider_field = form[self.provider_field]

        if self.query_attrs:
            query_field.field.widget.attrs.update(self.query_attrs)

        if self.provider_attrs:
            provider_field.field.widget.attrs.update(self.provider_attrs)

        return render_to_string(
            "search/form_elements/query_input.html",
            {
                "query_field": query_field,
                "provider_field": provider_field,
            },
        )


class RangeSlider(TemplateNameMixin):
    """A range slider with two knobs.

    Inspired by:
    https://medium.com/@predragdavidovic10/native-dual-range-slider-html-css-javascript-91e778134816
    https://codepen.io/glitchworker/pen/XVdKqj
    https://tailwindcomponents.com/component/multi-range-slider
    """

    def __init__(
        self,
        legend: str,
        from_field: str,
        till_field: str,
        css_class: str | None = None,
        **kwargs,
    ) -> None:
        self.legend = legend
        self.from_field = from_field
        self.till_field = till_field
        self.attrs: dict[str, Any] = {}

        if css_class:
            self.attrs["class"] = css_class

        self.attrs.update({k.replace("_", "-"): conditional_escape(v) for k, v in kwargs.items()})

    def render(self, form: Form, context: dict[str, Any], *args, **kwargs) -> str:
        return render_to_string(
            "search/form_elements/range_slider.html",
            {
                "legend": self.legend,
                "from_field": form[self.from_field],
                "till_field": form[self.till_field],
            },
        )
