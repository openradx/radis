from typing import Any

from crispy_forms.layout import TemplateNameMixin
from django.forms import Form
from django.template.loader import render_to_string
from django.utils.html import conditional_escape


class QueryInput(TemplateNameMixin):
    def __init__(
        self,
        query_field: str,
        algorithm_field: str,
        css_class: str | None = None,
        query_attrs: dict[str, Any] | None = None,
        algorithm_attrs: dict[str, Any] | None = None,
        **kwargs,
    ) -> None:
        self.query_field = query_field
        self.algorithm_field = algorithm_field

        self.query_attrs: dict[str, Any] = {}
        self.algorithm_attrs: dict[str, Any] = {}

        if css_class:
            self.query_attrs["class"] = css_class
            self.algorithm_attrs["class"] = css_class

        if query_attrs:
            self.query_attrs.update(query_attrs)

        if algorithm_attrs:
            self.algorithm_attrs.update(algorithm_attrs)

        self.query_attrs.update(
            {k.replace("_", "-"): conditional_escape(v) for k, v in kwargs.items()}
        )
        self.algorithm_attrs.update(
            {k.replace("_", "-"): conditional_escape(v) for k, v in kwargs.items()}
        )

    def render(self, form: Form, context: dict[str, Any], *args, **kwargs) -> str:
        query_field = form[self.query_field]
        algorithm_field = form[self.algorithm_field]

        if self.query_attrs:
            query_field.field.widget.attrs.update(self.query_attrs)

        if self.algorithm_attrs:
            algorithm_field.field.widget.attrs.update(self.algorithm_attrs)

        return render_to_string(
            "search/crispy_forms_layout/query_input.html",
            {
                "query_field": query_field,
                "algorithm_field": algorithm_field,
            },
        )


class DateRange(TemplateNameMixin):
    def __init__(
        self,
        legend: str,
        from_field: str,
        till_field: str,
        css_class: str | None = None,
        group_class: str | None = None,
        wrapper_class: str | None = None,
        **kwargs,
    ) -> None:
        self.legend = legend
        self.from_field = from_field
        self.till_field = till_field
        self.group_class = group_class
        self.wrapper_class = wrapper_class
        self.attrs: dict[str, Any] = {}

        if css_class:
            self.attrs["class"] = css_class

        self.attrs.update({k.replace("_", "-"): conditional_escape(v) for k, v in kwargs.items()})

    def render(self, form: Form, context: dict[str, Any], *args, **kwargs) -> str:
        from_field = form[self.from_field]
        till_field = form[self.till_field]

        if self.attrs:
            from_field.field.widget.attrs.update(self.attrs)
            till_field.field.widget.attrs.update(self.attrs)

        return render_to_string(
            "search/crispy_forms_layout/date_range.html",
            {
                "legend": self.legend,
                "from_field": from_field,
                "till_field": till_field,
                "group_class": self.group_class,
                "wrapper_class": self.wrapper_class,
            },
        )


class RangeSlider(TemplateNameMixin):
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
            "search/crispy_forms_layout/range_slider.html",
            {
                "legend": self.legend,
                "from_field": form[self.from_field],
                "till_field": form[self.till_field],
            },
        )
