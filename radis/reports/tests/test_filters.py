import datetime

import pytest

from radis.reports.filters import ReportFilter
from radis.reports.models import Report


@pytest.mark.django_db
def test_report_filter_accepts_day_first_date():
    filterset = ReportFilter(
        data={"study_date_from": "14/02/2025"},
        queryset=Report.objects.none(),
    )
    assert filterset.form.is_valid()
    assert filterset.form.cleaned_data["study_date_from"] == datetime.date(2025, 2, 14)


@pytest.mark.django_db
def test_report_filter_accepts_iso_date():
    filterset = ReportFilter(
        data={"study_date_till": "2025-02-14"},
        queryset=Report.objects.none(),
    )
    assert filterset.form.is_valid()
    assert filterset.form.cleaned_data["study_date_till"] == datetime.date(2025, 2, 14)
