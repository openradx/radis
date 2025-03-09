from typing import Any

from crispy_forms.layout import LayoutObject
from django.forms import Form
from django.template.loader import render_to_string
from django.utils.html import conditional_escape


class Formset(LayoutObject):
    template = "cotton/formset.html"

    def __init__(self, formset_name_in_context, **kwargs):
        self.formset_name_in_context = formset_name_in_context
        self.kwargs = kwargs

    def render(self, form, context, template_pack=None, **kwargs):
        formset = context[self.formset_name_in_context]
        context.update({"formset": formset})
        context.update(self.kwargs)
        result = render_to_string(self.template, context.flatten())
        return result


class RangeSlider(LayoutObject):
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
