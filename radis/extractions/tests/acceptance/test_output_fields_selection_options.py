from urllib.parse import urlparse

import pytest
from adit_radis_shared.accounts.factories import GroupFactory, UserFactory
from django.contrib.auth.models import Permission
from django.test import Client
from playwright.sync_api import Page, expect
from pytest_django.live_server_helper import LiveServer

from radis.reports.models import Language, Modality


@pytest.mark.acceptance
@pytest.mark.order("last")
@pytest.mark.django_db(transaction=True)
def test_selection_options_work_for_added_output_field(live_server: LiveServer, page: Page):
    group = GroupFactory.create()
    user = UserFactory.create(is_active=True)
    user.groups.add(group)
    user.active_group = group
    user.user_permissions.add(Permission.objects.get(codename="add_extractionjob"))
    user.save()

    language = Language.objects.create(code="en")
    Modality.objects.create(code="CT", filterable=True)

    client = Client()
    client.force_login(user)

    hostname = urlparse(live_server.url).hostname
    assert hostname
    page.context.add_cookies(
        [
            {
                "name": "sessionid",
                "value": client.cookies["sessionid"].value,
                "domain": hostname,
                "path": "/",
            }
        ]
    )

    page.goto(f"{live_server.url}/extractions/jobs/new/")
    page.locator('input[name="0-title"]').fill("Test Extraction")
    page.locator('select[name="0-provider"]').select_option("PG Search")
    page.locator('input[name="0-query"]').fill("test")
    page.locator('select[name="0-language"]').select_option(str(language.pk))
    page.locator('input[type="submit"][value*="Next Step"]').click()

    page.get_by_role("button", name="Add Field").click()

    forms = page.locator(".formset-form")
    expect(forms).to_have_count(2)

    second_form = forms.nth(1)
    selection_input = second_form.locator("[data-selection-input]")
    expect(selection_input).to_have_count(1)
    expect(selection_input).to_have_attribute("data-max-selection-options", "7")
    second_form.locator('select[name$="-output_type"]').select_option("S")
    expect(second_form.locator('select[name$="-output_type"]')).to_have_value("S")
    add_option_button = second_form.get_by_role("button", name="Enter a selection")
    expect(add_option_button).to_be_enabled()
    add_option_button.click()

    expect(second_form.locator('input[placeholder="Selection 1"]')).to_be_visible()
