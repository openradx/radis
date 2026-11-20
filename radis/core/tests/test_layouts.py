import pytest
from crispy_forms.helper import FormHelper
from crispy_forms.layout import Layout
from crispy_forms.utils import render_crispy_form
from django import forms

from radis.core.form_fields import create_age_range_fields
from radis.core.layouts import RangeSlider


class _AgeForm(forms.Form):
    def __init__(self, *args, show_errors: bool, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.fields["age_from"], self.fields["age_till"] = create_age_range_fields()
        self.helper = FormHelper()
        self.helper.form_tag = False
        self.helper.form_show_errors = show_errors
        self.helper.layout = Layout(RangeSlider("Age range", "age_from", "age_till"))


@pytest.mark.parametrize("show_errors", [True, False])
def test_the_range_slider_shows_its_errors_only_when_the_helper_does(show_errors: bool) -> None:
    """A page that lists every field error elsewhere turns the helper's errors off."""
    form = _AgeForm({"age_from": "-1", "age_till": "abc"}, show_errors=show_errors)
    assert not form.is_valid()

    html = render_crispy_form(form, form.helper)

    for message in ("Ensure this value is greater than or equal to 0.", "Enter a whole number."):
        assert (message in html) is show_errors
