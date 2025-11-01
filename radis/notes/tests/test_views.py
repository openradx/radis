import pytest
from adit_radis_shared.accounts.factories import UserFactory
from django.test import Client
from django.urls import reverse

from radis.notes.factories import NoteFactory
from radis.notes.models import Note
from radis.reports.factories import LanguageFactory, ReportFactory


def create_test_report():
    language = LanguageFactory.create(code="en")
    return ReportFactory.create(language=language)


@pytest.mark.django_db
def test_note_list_view(client: Client):
    user = UserFactory.create(is_active=True)
    client.force_login(user)

    response = client.get(reverse("note_list"))
    assert response.status_code == 200


@pytest.mark.django_db
def test_note_list_view_with_notes(client: Client):
    user = UserFactory.create(is_active=True)
    report = create_test_report()
    note = NoteFactory.create(owner=user, report=report)
    client.force_login(user)

    response = client.get(reverse("note_list"))
    assert response.status_code == 200
    assert note in response.context["notes"]


@pytest.mark.django_db
def test_note_list_view_filters_by_owner(client: Client):
    user = UserFactory.create(is_active=True)
    other_user = UserFactory.create(is_active=True)
    report = create_test_report()

    user_note = NoteFactory.create(owner=user, report=report)
    other_note = NoteFactory.create(owner=other_user, report=report)

    client.force_login(user)

    response = client.get(reverse("note_list"))
    assert response.status_code == 200
    assert user_note in response.context["notes"]
    assert other_note not in response.context["notes"]


@pytest.mark.django_db
def test_note_detail_view(client: Client):
    user = UserFactory.create(is_active=True)
    report = create_test_report()
    note = NoteFactory.create(owner=user, report=report)
    client.force_login(user)

    response = client.get(reverse("note_detail", args=[note.pk]), HTTP_HX_REQUEST="true")
    assert response.status_code == 200


@pytest.mark.django_db
def test_note_detail_view_unauthorized(client: Client):
    user = UserFactory.create(is_active=True)
    other_user = UserFactory.create(is_active=True)
    report = create_test_report()
    note = NoteFactory.create(owner=other_user, report=report)
    client.force_login(user)

    response = client.get(reverse("note_detail", args=[note.pk]), HTTP_HX_REQUEST="true")
    assert response.status_code == 404  # Should not see other user's notes


@pytest.mark.django_db
def test_note_detail_view_requires_htmx(client: Client):
    user = UserFactory.create(is_active=True)
    report = create_test_report()
    note = NoteFactory.create(owner=user, report=report)
    client.force_login(user)

    response = client.get(reverse("note_detail", args=[note.pk]))
    assert response.status_code == 400  # Bad request without HTMX


@pytest.mark.django_db
def test_note_edit_view_get_existing_note(client: Client):
    user = UserFactory.create(is_active=True)
    report = create_test_report()
    NoteFactory.create(owner=user, report=report)
    client.force_login(user)

    response = client.get(reverse("note_edit", args=[report.pk]), HTTP_HX_REQUEST="true")
    assert response.status_code == 200
    assert response.context["report_id"] == report.pk


@pytest.mark.django_db
def test_note_edit_view_get_new_note(client: Client):
    user = UserFactory.create(is_active=True)
    report = create_test_report()
    client.force_login(user)

    response = client.get(reverse("note_edit", args=[report.pk]), HTTP_HX_REQUEST="true")
    assert response.status_code == 200
    assert response.context["report_id"] == report.pk


@pytest.mark.django_db
def test_note_edit_view_post_create_note(client: Client):
    user = UserFactory.create(is_active=True)
    report = create_test_report()
    client.force_login(user)

    response = client.post(
        reverse("note_edit", args=[report.pk]),
        {"text": "This is a test note"},
        HTTP_HX_REQUEST="true",
    )

    assert response.status_code == 204

    note = Note.objects.get(owner=user, report=report)
    assert note.text == "This is a test note"


@pytest.mark.django_db
def test_note_edit_view_post_update_note(client: Client):
    user = UserFactory.create(is_active=True)
    report = create_test_report()
    note = NoteFactory.create(owner=user, report=report, text="Original text")
    client.force_login(user)

    response = client.post(
        reverse("note_edit", args=[report.pk]), {"text": "Updated text"}, HTTP_HX_REQUEST="true"
    )
    assert response.status_code == 204
    note.refresh_from_db()
    assert note.text == "Updated text"


@pytest.mark.django_db
def test_note_edit_view_post_delete_note_empty_text(client: Client):
    user = UserFactory.create(is_active=True)
    report = create_test_report()
    NoteFactory.create(owner=user, report=report, text="Some text")
    client.force_login(user)

    response = client.post(
        reverse("note_edit", args=[report.pk]), {"text": ""}, HTTP_HX_REQUEST="true"
    )
    assert response.status_code == 204

    assert not Note.objects.filter(owner=user, report=report).exists()


@pytest.mark.django_db
def test_note_edit_view_post_delete_note_whitespace_only(client: Client):
    user = UserFactory.create(is_active=True)
    report = create_test_report()
    NoteFactory.create(owner=user, report=report, text="Some text")
    client.force_login(user)

    response = client.post(
        reverse("note_edit", args=[report.pk]), {"text": "   \n\t   "}, HTTP_HX_REQUEST="true"
    )
    assert response.status_code == 204

    assert not Note.objects.filter(owner=user, report=report).exists()


@pytest.mark.django_db
def test_note_edit_view_requires_htmx(client: Client):
    user = UserFactory.create(is_active=True)
    report = create_test_report()
    client.force_login(user)

    response = client.get(reverse("note_edit", args=[report.pk]))
    assert response.status_code == 400  # Bad request without HTMX


@pytest.mark.django_db
def test_note_available_badge_view(client: Client):
    user = UserFactory.create(is_active=True)
    report = create_test_report()
    client.force_login(user)

    response = client.get(reverse("note_available_badge", args=[report.pk]), HTTP_HX_REQUEST="true")
    assert response.status_code == 200
    assert response.context["report"] == report


@pytest.mark.django_db
def test_note_available_badge_view_requires_htmx(client: Client):
    user = UserFactory.create(is_active=True)
    report = create_test_report()
    client.force_login(user)

    response = client.get(reverse("note_available_badge", args=[report.pk]))
    assert response.status_code == 400  # Bad request without HTMX


@pytest.mark.django_db
def test_note_list_view_unauthenticated(client: Client):
    response = client.get(reverse("note_list"))
    assert response.status_code == 302
    assert "/accounts/login/" in response["Location"]


@pytest.mark.django_db
def test_note_detail_view_unauthenticated(client: Client):
    report = create_test_report()
    note = NoteFactory.create(report=report)

    response = client.get(reverse("note_detail", args=[note.pk]), HTTP_HX_REQUEST="true")
    assert response.status_code == 302
    assert "/accounts/login/" in response["Location"]


@pytest.mark.django_db
def test_note_edit_view_unauthenticated(client: Client):
    report = create_test_report()

    response = client.get(reverse("note_edit", args=[report.pk]), HTTP_HX_REQUEST="true")
    assert response.status_code == 302
    assert "/accounts/login/" in response["Location"]


@pytest.mark.django_db
def test_note_available_badge_view_unauthenticated(client: Client):
    report = create_test_report()

    response = client.get(reverse("note_available_badge", args=[report.pk]), HTTP_HX_REQUEST="true")
    assert response.status_code == 302
    assert "/accounts/login/" in response["Location"]


@pytest.mark.django_db
def test_note_list_view_pagination(client: Client):
    user = UserFactory.create(is_active=True)

    reports = [create_test_report() for _ in range(15)]
    [NoteFactory.create(owner=user, report=report) for report in reports]

    client.force_login(user)

    response = client.get(reverse("note_list"))
    assert response.status_code == 200
    assert len(response.context["notes"]) == 10

    response = client.get(reverse("note_list") + "?page=2")
    assert response.status_code == 200
    assert len(response.context["notes"]) == 5


@pytest.mark.django_db
def test_note_list_view_page_size_change(client: Client):
    user = UserFactory.create(is_active=True)

    reports = [create_test_report() for _ in range(30)]
    [NoteFactory.create(owner=user, report=report) for report in reports]

    client.force_login(user)

    # Test with page size 25 (using per_page parameter)
    response = client.get(reverse("note_list") + "?per_page=25")
    assert response.status_code == 200
    assert len(response.context["notes"]) == 25

    # Test with page size 50 (using per_page parameter)
    response = client.get(reverse("note_list") + "?per_page=50")
    assert response.status_code == 200
    assert len(response.context["notes"]) == 30
