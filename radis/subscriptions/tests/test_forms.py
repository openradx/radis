"""Tests for SubscriptionForm age validators.

AGE_STEP=10, MIN_AGE=0, MAX_AGE=120 (radis.search.forms). The form enforces:
- age_from / age_till must be multiples of AGE_STEP (when provided)
- age_from must be strictly less than age_till (when both provided)
- both are optional (None passes)
"""

import pytest
from adit_radis_shared.accounts.factories import GroupFactory, UserFactory

from radis.reports.factories import LanguageFactory
from radis.search.forms import AGE_STEP
from radis.subscriptions.forms import SubscriptionForm


def _form_data(**overrides) -> dict:
    data = {
        "name": "My Subscription",
        "query": "pneumonia",
        "language": "",
        "modalities": [],
        "study_description": "",
        "patient_sex": "",
        "patient_id": "",
        "age_from": "",
        "age_till": "",
        "send_finished_mail": False,
    }
    data.update(overrides)
    return data


@pytest.fixture
def _ref_data(db):
    # The form's __init__ queries Language/Modality choices.
    LanguageFactory.create(code="en")
    UserFactory.create()
    GroupFactory.create()


@pytest.mark.django_db
def test_valid_age_range_passes(_ref_data):
    form = SubscriptionForm(data=_form_data(age_from=20, age_till=60))
    assert form.is_valid(), form.errors


@pytest.mark.django_db
def test_blank_ages_pass(_ref_data):
    form = SubscriptionForm(data=_form_data(age_from="", age_till=""))
    assert form.is_valid(), form.errors


@pytest.mark.django_db
def test_age_from_not_multiple_of_step_is_rejected(_ref_data):
    form = SubscriptionForm(data=_form_data(age_from=25, age_till=60))
    assert not form.is_valid()
    assert "age_from" in form.errors
    assert f"multiple of {AGE_STEP}" in str(form.errors["age_from"])


@pytest.mark.django_db
def test_age_till_not_multiple_of_step_is_rejected(_ref_data):
    form = SubscriptionForm(data=_form_data(age_from=20, age_till=55))
    assert not form.is_valid()
    assert "age_till" in form.errors
    assert f"multiple of {AGE_STEP}" in str(form.errors["age_till"])


@pytest.mark.django_db
def test_age_from_must_be_less_than_age_till(_ref_data):
    form = SubscriptionForm(data=_form_data(age_from=60, age_till=60))
    assert not form.is_valid()
    # Cross-field error lands in non-field errors.
    assert "Age from must be less than age till" in str(form.errors)


@pytest.mark.django_db
def test_age_from_greater_than_age_till_is_rejected(_ref_data):
    form = SubscriptionForm(data=_form_data(age_from=80, age_till=40))
    assert not form.is_valid()
    assert "Age from must be less than age till" in str(form.errors)
