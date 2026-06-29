"""Browser acceptance tests: the surfacing badge and the search filter control render in a real
browser. The logic is covered without a browser (test_surfacing.py, test_search_filter.py) and
the pipeline by test_labeling_integration.py, so these seed rows directly and stay minimal.
"""

import pytest
from adit_radis_shared.accounts.factories import GroupFactory, UserFactory
from adit_radis_shared.common.utils.testing_helpers import add_user_to_group, login_user
from playwright.sync_api import Page, expect
from pytest_django.live_server_helper import LiveServer

from radis.labels.factories import LabelFactory, LabelGroupFactory, LabelResultFactory
from radis.labels.models import LabelResult
from radis.reports.factories import ReportFactory

PASSWORD = "test_secret_secret"


def _user_with_active_group():
    user = UserFactory.create(password=PASSWORD, is_active=True)
    group = GroupFactory.create()
    add_user_to_group(user, group, force_activate_group=True)
    return user, group


@pytest.mark.acceptance
@pytest.mark.order("last")
@pytest.mark.django_db(transaction=True)
def test_surfacing_label_badge_visible_on_report_detail(live_server: LiveServer, page: Page):
    """A PRESENT label renders as a badge, under its group name, on the report detail page."""
    user, group = _user_with_active_group()

    label_group = LabelGroupFactory.create(name="Chest")
    label = LabelFactory.create(group=label_group, name="pneumonia")
    report = ReportFactory.create()
    report.groups.add(group)
    LabelResultFactory.create(report=report, label=label, value=LabelResult.Value.PRESENT)

    login_user(page, live_server.url, user.username, PASSWORD)
    page.goto(f"{live_server.url}/reports/{report.pk}/")

    labels = page.locator(".report-labels")
    expect(labels.locator(".badge", has_text="pneumonia")).to_be_visible()
    expect(labels).to_contain_text("Chest")


@pytest.mark.acceptance
@pytest.mark.order("last")
@pytest.mark.django_db(transaction=True)
def test_active_label_offered_in_search_filters_panel(live_server: LiveServer, page: Page):
    """An active label is selectable in the search Filters panel's label multi-select."""
    user, _ = _user_with_active_group()
    LabelFactory.create(name="pneumonia")  # active -> becomes a filter choice

    login_user(page, live_server.url, user.username, PASSWORD)
    page.goto(f"{live_server.url}/search/")

    option = page.locator('select[name="labels"] option', has_text="pneumonia")
    expect(option).to_have_count(1)
