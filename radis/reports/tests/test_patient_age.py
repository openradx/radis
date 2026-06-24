"""DB tests for the ``Report.patient_age`` generated column.

``patient_age`` is a Postgres-persisted ``GeneratedField`` (see
``reports/models.py``) backed by the ``calc_age`` SQL function from migration
``0011_report_patient_age.py``:

    EXTRACT(YEAR FROM age(study_datetime, patient_birth_date::timestamp))

i.e. the number of *completed* years between birth and the study, evaluated at
study time (age-at-study, not age-today). These tests insert reports with known
birth dates / study datetimes and assert the computed value after
``refresh_from_db``.
"""

from datetime import date, datetime, timezone

import pytest

from radis.reports.factories import LanguageFactory, ReportFactory


def _make_report(birth_date: date, study_dt: datetime):
    language = LanguageFactory.create(code="en")
    return ReportFactory.create(
        language=language,
        patient_birth_date=birth_date,
        study_datetime=study_dt,
    )


@pytest.mark.django_db
def test_patient_age_basic_completed_years():
    report = _make_report(
        date(1980, 1, 15),
        datetime(2024, 3, 15, 10, 30, tzinfo=timezone.utc),
    )
    report.refresh_from_db()
    # Born 1980-01-15, study 2024-03-15 -> 44th birthday already passed.
    assert report.patient_age == 44


@pytest.mark.django_db
def test_patient_age_day_before_birthday():
    """Study one day before the birthday -> age has NOT yet ticked over."""
    report = _make_report(
        date(1990, 6, 10),
        datetime(2024, 6, 9, 8, 0, tzinfo=timezone.utc),
    )
    report.refresh_from_db()
    # 2024-06-09 is the day before the 34th birthday -> still 33.
    assert report.patient_age == 33


@pytest.mark.django_db
def test_patient_age_on_birthday():
    """Study exactly on the birthday -> age ticks over that day."""
    report = _make_report(
        date(1990, 6, 10),
        datetime(2024, 6, 10, 0, 1, tzinfo=timezone.utc),
    )
    report.refresh_from_db()
    assert report.patient_age == 34


@pytest.mark.django_db
def test_patient_age_day_after_birthday():
    report = _make_report(
        date(1990, 6, 10),
        datetime(2024, 6, 11, 8, 0, tzinfo=timezone.utc),
    )
    report.refresh_from_db()
    assert report.patient_age == 34


@pytest.mark.django_db
def test_patient_age_leap_year_birthday_before_feb29_nonleap():
    """Born on a leap day (Feb 29). In a non-leap study year, the 'birthday'
    effectively lands on Mar 1, so Feb 28 of that year is still the prior age.
    """
    # Born 2000-02-29. Study 2023-02-28 (2023 is not a leap year).
    report = _make_report(
        date(2000, 2, 29),
        datetime(2023, 2, 28, 12, 0, tzinfo=timezone.utc),
    )
    report.refresh_from_db()
    # Postgres age(): on 2023-02-28 the patient is still 22 (turns 23 on 03-01).
    assert report.patient_age == 22


@pytest.mark.django_db
def test_patient_age_leap_year_birthday_on_feb29_leap_study():
    """Born Feb 29, study on a real Feb 29 in a leap year -> birthday hits."""
    report = _make_report(
        date(2000, 2, 29),
        datetime(2024, 2, 29, 9, 0, tzinfo=timezone.utc),
    )
    report.refresh_from_db()
    assert report.patient_age == 24


@pytest.mark.django_db
def test_patient_age_recomputed_on_update():
    """The persisted generated column must be recomputed when inputs change."""
    report = _make_report(
        date(1980, 1, 15),
        datetime(2000, 1, 15, 10, 0, tzinfo=timezone.utc),
    )
    report.refresh_from_db()
    assert report.patient_age == 20

    report.study_datetime = datetime(2024, 1, 15, 10, 0, tzinfo=timezone.utc)
    report.save()
    report.refresh_from_db()
    assert report.patient_age == 44


@pytest.mark.django_db
def test_patient_age_same_day_birth_and_study_is_zero():
    report = _make_report(
        date(2024, 5, 1),
        datetime(2024, 5, 1, 12, 0, tzinfo=timezone.utc),
    )
    report.refresh_from_db()
    assert report.patient_age == 0
