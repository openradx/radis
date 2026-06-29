import re

import pytest
from adit_radis_shared.accounts.factories import GroupFactory, UserFactory
from adit_radis_shared.common.utils.testing_helpers import (
    add_permission,
    add_user_to_group,
    create_and_login_example_user,
    login_user,
)
from playwright.sync_api import Page, expect
from pytest_django.live_server_helper import LiveServer

from radis.extractions.factories import ExtractionJobFactory, OutputFieldFactory
from radis.extractions.models import ExtractionJob


def setup_extraction_user(page: Page, server_url: str):
    """Create a user with extraction permissions and an active group."""
    user = create_and_login_example_user(page, server_url)
    group = GroupFactory.create()
    add_user_to_group(user, group, force_activate_group=True)
    add_permission(user, "extractions", "add_extractionjob")
    return user, group


def hide_debug_toolbar(page: Page):
    """Hide Django Debug Toolbar to prevent it from intercepting clicks."""
    page.evaluate("document.getElementById('djDebug')?.remove()")


@pytest.mark.acceptance
@pytest.mark.order("last")
@pytest.mark.django_db(transaction=True)
def test_extraction_job_list_accessible(live_server: LiveServer, page: Page):
    """Test that the extraction job list page is accessible."""
    create_and_login_example_user(page, live_server.url)
    page.goto(live_server.url + "/extractions/jobs/")
    expect(page).to_have_title(re.compile("Extraction Jobs"))


@pytest.mark.acceptance
@pytest.mark.order("last")
@pytest.mark.django_db(transaction=True)
def test_extraction_job_list_shows_jobs(live_server: LiveServer, page: Page):
    """Test that the extraction job list displays created jobs."""
    user, group = setup_extraction_user(page, live_server.url)

    job1 = ExtractionJobFactory.create(owner=user, group=group, title="Listed Job One")
    job2 = ExtractionJobFactory.create(owner=user, group=group, title="Listed Job Two")

    page.goto(live_server.url + "/extractions/jobs/")

    # The job table shows ID, status, message, created_at columns (no title column)
    # Verify jobs appear by checking the table has link elements with job IDs
    table = page.locator("#analysis_job_table")
    expect(table.get_by_role("link", name=str(job1.pk))).to_be_visible()
    expect(table.get_by_role("link", name=str(job2.pk))).to_be_visible()


@pytest.mark.acceptance
@pytest.mark.order("last")
@pytest.mark.django_db(transaction=True)
def test_extraction_wizard_step1_loads(live_server: LiveServer, page: Page):
    """Test that the extraction wizard step 1 loads correctly."""
    setup_extraction_user(page, live_server.url)

    page.goto(live_server.url + "/extractions/jobs/new/")
    expect(page).to_have_title(re.compile("New Extraction Job"))
    expect(page.get_by_text("Define Fields (Step 1 of 3)")).to_be_visible()

    # Verify the output field form inputs are present
    expect(page.locator("#id_0-0-name")).to_be_visible()
    expect(page.locator("#id_0-0-description")).to_be_visible()

    # Verify navigation button
    expect(page.get_by_role("button", name="Next Step (Search Query)")).to_be_visible()


@pytest.mark.acceptance
@pytest.mark.order("last")
@pytest.mark.django_db(transaction=True)
def test_extraction_wizard_step1_fill_fields(live_server: LiveServer, page: Page):
    """Test filling in output fields in wizard step 1."""
    setup_extraction_user(page, live_server.url)

    page.goto(live_server.url + "/extractions/jobs/new/")

    # Fill in the first output field
    page.locator("#id_0-0-name").fill("finding_type")
    page.locator("#id_0-0-description").fill("The type of finding in the report")

    # Verify the values are filled
    expect(page.locator("#id_0-0-name")).to_have_value("finding_type")
    expect(page.locator("#id_0-0-description")).to_have_value(
        "The type of finding in the report"
    )


@pytest.mark.acceptance
@pytest.mark.order("last")
@pytest.mark.django_db(transaction=True)
def test_extraction_wizard_step1_to_step2(live_server: LiveServer, page: Page):
    """Test navigating from wizard step 1 to step 2."""
    setup_extraction_user(page, live_server.url)

    page.goto(live_server.url + "/extractions/jobs/new/")

    # Fill step 1
    page.locator("#id_0-0-name").fill("finding_type")
    page.locator("#id_0-0-description").fill("The type of finding in the report")
    page.get_by_role("button", name="Next Step (Search Query)").click()

    # Should be on step 2
    expect(page.get_by_text("Search Query (Step 2 of 3)")).to_be_visible()
    expect(page.get_by_role("button", name="Next Step (Summary)")).to_be_visible()


@pytest.mark.acceptance
@pytest.mark.order("last")
@pytest.mark.django_db(transaction=True)
def test_extraction_wizard_step1_validation(live_server: LiveServer, page: Page):
    """Test that step 1 requires at least one output field with name and description."""
    setup_extraction_user(page, live_server.url)

    page.goto(live_server.url + "/extractions/jobs/new/")

    # Try to submit without filling any fields
    page.get_by_role("button", name="Next Step (Search Query)").click()

    # Should stay on step 1 with validation errors
    expect(page.get_by_text("Define Fields (Step 1 of 3)")).to_be_visible()


@pytest.mark.acceptance
@pytest.mark.order("last")
@pytest.mark.django_db(transaction=True)
def test_extraction_job_detail(live_server: LiveServer, page: Page):
    """Test viewing an extraction job's detail page."""
    user, group = setup_extraction_user(page, live_server.url)

    # Create job via factory
    job = ExtractionJobFactory.create(
        owner=user,
        group=group,
        title="Test Detail Job",
    )
    OutputFieldFactory.create(
        job=job,
        subscription=None,
        name="test_field",
        description="A test output field",
    )

    page.goto(live_server.url + f"/extractions/jobs/{job.pk}/")
    expect(page).to_have_title(re.compile("Extraction Job"))
    expect(page.get_by_text("Test Detail Job")).to_be_visible()
    expect(page.get_by_text("test_field")).to_be_visible()
    expect(page.get_by_text("A test output field")).to_be_visible()

    # Verify general info section
    expect(page.get_by_text("Job ID")).to_be_visible()
    expect(page.get_by_text("Job Title")).to_be_visible()

    # Verify output fields section
    expect(page.get_by_text("Output Fields")).to_be_visible()


@pytest.mark.acceptance
@pytest.mark.order("last")
@pytest.mark.django_db(transaction=True)
def test_extraction_job_detail_shows_search_params(
    live_server: LiveServer, page: Page
):
    """Test that job detail shows search parameters."""
    user, group = setup_extraction_user(page, live_server.url)

    job = ExtractionJobFactory.create(
        owner=user,
        group=group,
        title="Params Job",
        query="pneumonia CT",
        study_description="CT Thorax",
        patient_sex="M",
    )

    page.goto(live_server.url + f"/extractions/jobs/{job.pk}/")
    expect(page.get_by_text("Search parameters")).to_be_visible()
    expect(page.get_by_text("pneumonia CT")).to_be_visible()
    expect(page.get_by_text("CT Thorax")).to_be_visible()


@pytest.mark.acceptance
@pytest.mark.order("last")
@pytest.mark.django_db(transaction=True)
def test_extraction_job_delete(live_server: LiveServer, page: Page):
    """Test deleting an extraction job from its detail page."""
    user, group = setup_extraction_user(page, live_server.url)

    job = ExtractionJobFactory.create(
        owner=user,
        group=group,
        title="Job To Delete",
    )
    job_pk = job.pk

    page.goto(live_server.url + f"/extractions/jobs/{job_pk}/")
    expect(page.get_by_text("Job To Delete")).to_be_visible()

    # Hide debug toolbar to prevent it from intercepting clicks
    hide_debug_toolbar(page)

    # The delete button has a JS confirmation dialog
    page.on("dialog", lambda dialog: dialog.accept())
    page.get_by_role("button", name="Delete Job").click()

    # Should redirect to job list
    expect(page).to_have_url(re.compile(r"/extractions/jobs/$"))
    assert not ExtractionJob.objects.filter(pk=job_pk).exists()


@pytest.mark.acceptance
@pytest.mark.order("last")
@pytest.mark.django_db(transaction=True)
def test_extraction_job_other_user_cannot_view(live_server: LiveServer, page: Page):
    """Test that a user cannot view another user's extraction job."""
    user1, group1 = setup_extraction_user(page, live_server.url)

    job = ExtractionJobFactory.create(
        owner=user1,
        group=group1,
        title="Private Job",
    )

    # Login as a different user
    password = "another_secret"
    user2 = UserFactory.create(password=password, is_active=True)
    login_user(page, live_server.url, user2.username, password)

    # Try to access the job detail
    response = page.goto(live_server.url + f"/extractions/jobs/{job.pk}/")
    assert response.status == 404


@pytest.mark.acceptance
@pytest.mark.order("last")
@pytest.mark.django_db(transaction=True)
def test_extraction_wizard_requires_permission(live_server: LiveServer, page: Page):
    """Test that the extraction wizard requires add_extractionjob permission."""
    user = create_and_login_example_user(page, live_server.url)
    group = GroupFactory.create()
    add_user_to_group(user, group, force_activate_group=True)
    # Deliberately NOT adding add_extractionjob permission

    page.goto(live_server.url + "/extractions/jobs/new/")

    # Should get a 403 forbidden page
    expect(page.get_by_text("403", exact=True)).to_be_visible()


@pytest.mark.acceptance
@pytest.mark.order("last")
@pytest.mark.django_db(transaction=True)
def test_extraction_create_new_job_button(live_server: LiveServer, page: Page):
    """Test that the 'Create New Job' button on the job list page works."""
    setup_extraction_user(page, live_server.url)

    page.goto(live_server.url + "/extractions/jobs/")

    # Hide debug toolbar to prevent it from intercepting clicks
    hide_debug_toolbar(page)

    page.get_by_role("link", name="Create New Job").click()

    expect(page).to_have_url(re.compile(r"/extractions/jobs/new/"))
    expect(page.get_by_text("Define Fields (Step 1 of 3)")).to_be_visible()
