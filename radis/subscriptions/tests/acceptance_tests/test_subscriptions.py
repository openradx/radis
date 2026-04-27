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

from radis.reports.factories import ReportFactory
from radis.subscriptions.models import SubscribedItem, Subscription


def setup_subscription_user(page: Page, server_url: str):
    """Create a user with subscription permissions and an active group."""
    user = create_and_login_example_user(page, server_url)
    group = GroupFactory.create()
    add_user_to_group(user, group, force_activate_group=True)
    add_permission(user, "subscriptions", "add_subscription")
    return user, group


def hide_debug_toolbar(page: Page):
    """Hide Django Debug Toolbar to prevent it from intercepting clicks."""
    page.evaluate("document.getElementById('djDebug')?.remove()")


@pytest.mark.acceptance
@pytest.mark.order("last")
@pytest.mark.django_db(transaction=True)
def test_subscription_list_accessible(live_server: LiveServer, page: Page):
    """Test that the subscription list page is accessible after login."""
    create_and_login_example_user(page, live_server.url)
    page.goto(live_server.url + "/subscriptions/")
    expect(page).to_have_title(re.compile("Subscriptions"))


@pytest.mark.acceptance
@pytest.mark.order("last")
@pytest.mark.django_db(transaction=True)
def test_create_subscription(live_server: LiveServer, page: Page):
    """Test creating a new subscription with minimal fields."""
    user, group = setup_subscription_user(page, live_server.url)

    page.goto(live_server.url + "/subscriptions/create/")
    expect(page).to_have_title(re.compile("Create Subscription"))

    page.locator("#id_name").fill("My Test Subscription")
    page.get_by_role("button", name="Create Subscription").click()

    # Should redirect to subscription list
    expect(page).to_have_url(re.compile(r"/subscriptions/$"))
    expect(page.get_by_text("My Test Subscription")).to_be_visible()

    # Verify the subscription was created in the database
    assert Subscription.objects.filter(name="My Test Subscription", owner=user).exists()


@pytest.mark.acceptance
@pytest.mark.order("last")
@pytest.mark.django_db(transaction=True)
def test_create_subscription_with_filter_question(live_server: LiveServer, page: Page):
    """Test creating a subscription with a filter question."""
    user, group = setup_subscription_user(page, live_server.url)

    page.goto(live_server.url + "/subscriptions/create/")
    page.locator("#id_name").fill("Sub With Filter")

    # Fill filter question
    page.locator("#id_filter_questions-0-question").fill(
        "Does the report mention pneumonia?"
    )
    page.locator("#id_filter_questions-0-expected_answer").select_option("Y")

    page.get_by_role("button", name="Create Subscription").click()

    expect(page).to_have_url(re.compile(r"/subscriptions/$"))
    expect(page.get_by_text("Sub With Filter")).to_be_visible()

    # Verify filter question was saved
    sub = Subscription.objects.get(name="Sub With Filter", owner=user)
    assert sub.filter_questions.count() == 1
    question = sub.filter_questions.first()
    assert question.question == "Does the report mention pneumonia?"
    assert question.expected_answer == "Y"


@pytest.mark.acceptance
@pytest.mark.order("last")
@pytest.mark.django_db(transaction=True)
def test_create_subscription_with_output_field(live_server: LiveServer, page: Page):
    """Test creating a subscription with an extraction output field."""
    user, group = setup_subscription_user(page, live_server.url)

    page.goto(live_server.url + "/subscriptions/create/")
    page.locator("#id_name").fill("Sub With Output")

    # Fill extraction output field
    page.locator("#id_output_fields-0-name").fill("finding_size")
    page.locator("#id_output_fields-0-description").fill(
        "The size of the largest finding in mm"
    )

    page.get_by_role("button", name="Create Subscription").click()

    expect(page).to_have_url(re.compile(r"/subscriptions/$"))
    expect(page.get_by_text("Sub With Output")).to_be_visible()

    # Verify output field was saved
    sub = Subscription.objects.get(name="Sub With Output", owner=user)
    assert sub.output_fields.count() == 1
    field = sub.output_fields.first()
    assert field.name == "finding_size"
    assert field.description == "The size of the largest finding in mm"


@pytest.mark.acceptance
@pytest.mark.order("last")
@pytest.mark.django_db(transaction=True)
def test_create_subscription_without_permission(live_server: LiveServer, page: Page):
    """Test that a user without add_subscription permission is denied access."""
    user = create_and_login_example_user(page, live_server.url)
    group = GroupFactory.create()
    add_user_to_group(user, group, force_activate_group=True)
    # Deliberately NOT adding add_subscription permission

    page.goto(live_server.url + "/subscriptions/create/")

    # Should get a 403 forbidden page
    expect(page.get_by_text("403", exact=True)).to_be_visible()


@pytest.mark.acceptance
@pytest.mark.order("last")
@pytest.mark.django_db(transaction=True)
def test_view_subscription_detail(live_server: LiveServer, page: Page):
    """Test viewing a subscription's detail page."""
    user, group = setup_subscription_user(page, live_server.url)

    # Create subscription via UI
    page.goto(live_server.url + "/subscriptions/create/")
    page.locator("#id_name").fill("Detail Test Sub")
    page.get_by_role("button", name="Create Subscription").click()

    sub = Subscription.objects.get(name="Detail Test Sub", owner=user)

    # Navigate to detail page
    page.goto(live_server.url + f"/subscriptions/{sub.pk}/")
    expect(page).to_have_title(re.compile("Detail Test Sub"))
    expect(page.get_by_text("Subscription Title")).to_be_visible()
    expect(page.get_by_text("Detail Test Sub", exact=True)).to_be_visible()
    expect(page.get_by_role("link", name=re.compile("Edit"))).to_be_visible()
    expect(page.get_by_role("button", name=re.compile("Delete"))).to_be_visible()


@pytest.mark.acceptance
@pytest.mark.order("last")
@pytest.mark.django_db(transaction=True)
def test_view_subscription_detail_shows_filter_questions(
    live_server: LiveServer, page: Page
):
    """Test that subscription detail displays filter questions."""
    user, group = setup_subscription_user(page, live_server.url)

    # Create subscription with filter question
    page.goto(live_server.url + "/subscriptions/create/")
    page.locator("#id_name").fill("Detail With Questions")
    page.locator("#id_filter_questions-0-question").fill("Is there a fracture?")
    page.locator("#id_filter_questions-0-expected_answer").select_option("Y")
    page.get_by_role("button", name="Create Subscription").click()

    sub = Subscription.objects.get(name="Detail With Questions", owner=user)
    page.goto(live_server.url + f"/subscriptions/{sub.pk}/")

    expect(page.get_by_text("Filter Questions")).to_be_visible()
    expect(page.get_by_text("Is there a fracture?")).to_be_visible()


@pytest.mark.acceptance
@pytest.mark.order("last")
@pytest.mark.django_db(transaction=True)
def test_update_subscription(live_server: LiveServer, page: Page):
    """Test updating a subscription's name."""
    user, group = setup_subscription_user(page, live_server.url)

    # Create subscription
    page.goto(live_server.url + "/subscriptions/create/")
    page.locator("#id_name").fill("Original Name")
    page.get_by_role("button", name="Create Subscription").click()

    sub = Subscription.objects.get(name="Original Name", owner=user)

    # Navigate to update page
    page.goto(live_server.url + f"/subscriptions/{sub.pk}/update/")
    expect(page).to_have_title(re.compile("Update Subscription"))

    name_input = page.locator("#id_name")
    name_input.clear()
    name_input.fill("Updated Name")
    page.get_by_role("button", name="Update Subscription").click()

    # Should redirect to detail page
    expect(page).to_have_url(re.compile(rf"/subscriptions/{sub.pk}/$"))
    expect(page.get_by_text("Updated Name", exact=True)).to_be_visible()

    sub.refresh_from_db()
    assert sub.name == "Updated Name"


@pytest.mark.acceptance
@pytest.mark.order("last")
@pytest.mark.django_db(transaction=True)
def test_delete_subscription(live_server: LiveServer, page: Page):
    """Test deleting a subscription from its detail page."""
    user, group = setup_subscription_user(page, live_server.url)

    # Create subscription
    page.goto(live_server.url + "/subscriptions/create/")
    page.locator("#id_name").fill("To Be Deleted")
    page.get_by_role("button", name="Create Subscription").click()

    sub_pk = Subscription.objects.get(name="To Be Deleted", owner=user).pk

    # Navigate to detail and delete
    page.goto(live_server.url + f"/subscriptions/{sub_pk}/")
    hide_debug_toolbar(page)
    page.on("dialog", lambda dialog: dialog.accept())
    page.get_by_role("button", name=re.compile("Delete")).click()

    # Should redirect to list
    expect(page).to_have_url(re.compile(r"/subscriptions/$"))
    assert not Subscription.objects.filter(pk=sub_pk).exists()


@pytest.mark.acceptance
@pytest.mark.order("last")
@pytest.mark.django_db(transaction=True)
def test_subscription_inbox(live_server: LiveServer, page: Page):
    """Test viewing the subscription inbox with subscribed items."""
    user, group = setup_subscription_user(page, live_server.url)

    # Create subscription and subscribed items directly in DB
    sub = Subscription.objects.create(name="Inbox Test", owner=user, group=group)
    report = ReportFactory.create(body="Unique report body for acceptance test")
    SubscribedItem.objects.create(subscription=sub, report=report)

    page.goto(live_server.url + f"/subscriptions/{sub.pk}/inbox/")
    expect(page).to_have_title(re.compile("Inbox"))
    expect(page.get_by_text("Unique report body for acceptance test")).to_be_visible()

    # Verify sorting controls are present
    expect(page.get_by_text("Newest First")).to_be_visible()
    expect(page.get_by_text("Oldest First")).to_be_visible()


@pytest.mark.acceptance
@pytest.mark.order("last")
@pytest.mark.django_db(transaction=True)
def test_subscription_inbox_empty(live_server: LiveServer, page: Page):
    """Test that an empty subscription inbox shows the empty state."""
    user, group = setup_subscription_user(page, live_server.url)

    sub = Subscription.objects.create(name="Empty Inbox", owner=user, group=group)

    page.goto(live_server.url + f"/subscriptions/{sub.pk}/inbox/")
    expect(page.get_by_text("No items found")).to_be_visible()


@pytest.mark.acceptance
@pytest.mark.order("last")
@pytest.mark.django_db(transaction=True)
def test_subscription_inbox_sorting(live_server: LiveServer, page: Page):
    """Test that inbox sorting controls work."""
    user, group = setup_subscription_user(page, live_server.url)

    sub = Subscription.objects.create(name="Sort Test", owner=user, group=group)
    report1 = ReportFactory.create(body="First report body text")
    report2 = ReportFactory.create(body="Second report body text")
    SubscribedItem.objects.create(subscription=sub, report=report1)
    SubscribedItem.objects.create(subscription=sub, report=report2)

    page.goto(live_server.url + f"/subscriptions/{sub.pk}/inbox/")

    # Both reports should be visible
    expect(page.get_by_text("First report body text")).to_be_visible()
    expect(page.get_by_text("Second report body text")).to_be_visible()

    # Click "Oldest First" sorting
    page.get_by_text("Oldest First").click()
    expect(page).to_have_url(re.compile(r"sort_by=created_at&order=asc"))


@pytest.mark.acceptance
@pytest.mark.order("last")
@pytest.mark.django_db(transaction=True)
def test_subscription_duplicate_name(live_server: LiveServer, page: Page):
    """Test that creating a subscription with a duplicate name shows an error."""
    user, group = setup_subscription_user(page, live_server.url)

    # Create first subscription
    page.goto(live_server.url + "/subscriptions/create/")
    page.locator("#id_name").fill("Duplicate Name")
    page.get_by_role("button", name="Create Subscription").click()
    expect(page).to_have_url(re.compile(r"/subscriptions/$"))

    # Try to create another with the same name
    page.goto(live_server.url + "/subscriptions/create/")
    page.locator("#id_name").fill("Duplicate Name")
    page.get_by_role("button", name="Create Subscription").click()

    # Should stay on the create page with an error
    expect(page.get_by_text("A subscription with this name already exists")).to_be_visible()


@pytest.mark.acceptance
@pytest.mark.order("last")
@pytest.mark.django_db(transaction=True)
def test_subscription_other_user_cannot_view(live_server: LiveServer, page: Page):
    """Test that a user cannot view another user's subscription."""
    # Create subscription as first user
    user1, group1 = setup_subscription_user(page, live_server.url)
    page.goto(live_server.url + "/subscriptions/create/")
    page.locator("#id_name").fill("Private Sub")
    page.get_by_role("button", name="Create Subscription").click()
    sub = Subscription.objects.get(name="Private Sub", owner=user1)

    # Login as a different user
    password = "another_secret"
    user2 = UserFactory.create(password=password, is_active=True)
    login_user(page, live_server.url, user2.username, password)

    # Try to access the subscription detail
    response = page.goto(live_server.url + f"/subscriptions/{sub.pk}/")
    assert response.status == 404
