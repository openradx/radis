import datetime

from django import forms

from radis.core.date_formats import DATE_HTML_INPUT_FORMAT, DATE_INPUT_FORMATS
from radis.core.templatetags.core_extras import display_date, display_datetime
from radis.core.widgets import DatePickerInput


def test_date_picker_input_uses_html5_type():
    widget = DatePickerInput()
    assert widget.input_type == "date"
    assert widget.format == DATE_HTML_INPUT_FORMAT


def test_date_picker_input_preserves_custom_attributes():
    widget = DatePickerInput(attrs={"class": "custom", "data-role": "picker"})
    html = widget.render("study_date", None)
    assert 'type="date"' in html
    assert 'class="custom"' in html
    assert 'data-role="picker"' in html


def test_date_picker_input_accepts_defined_formats():
    field = forms.DateField(widget=DatePickerInput(), input_formats=DATE_INPUT_FORMATS)
    assert field.clean("14/02/2025") == datetime.date(2025, 2, 14)
    assert field.clean("2025-02-14") == datetime.date(2025, 2, 14)


def test_display_date_filter_formats_value():
    value = datetime.date(2025, 2, 14)
    assert display_date(value) == "14/02/2025"


def test_display_datetime_filter_formats_value():
    value = datetime.datetime(2025, 2, 14, 9, 30)
    assert display_datetime(value) == "14/02/2025 09:30"


def test_display_filters_return_empty_for_missing_values():
    assert display_date(None) == ""
    assert display_datetime(None) == ""
