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


# ---------------------------------------------------------------------------
# Cross-user isolation on the edit/update path.
#
# A user must not be able to read, overwrite, or take ownership of another
# user's note on the same report. ``NoteEditView`` keys notes by ``report_id``
# only, so all of the assertions below probe that ownership boundary.
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_note_edit_view_get_does_not_leak_other_users_note():
    """GET on the edit dialog for a report where ONLY another user has a note
    must not pre-fill the current user's form with the other user's text.

    KNOWN BUG: ``NoteEditView.get_object`` does
    ``Note.objects.filter(report_id=report_id).first()`` with no owner filter,
    so the foreign note is loaded and its text rendered into the form.
    """
    user = UserFactory.create(is_active=True)
    other_user = UserFactory.create(is_active=True)
    report = create_test_report()
    NoteFactory.create(owner=other_user, report=report, text="OTHER USERS SECRET")

    client = Client()
    client.force_login(user)

    response = client.get(reverse("note_edit", args=[report.pk]), HTTP_HX_REQUEST="true")
    assert response.status_code == 200
    form = response.context["form"]
    assert form.initial.get("text") in (None, ""), (
        "edit form leaked another user's note text"
    )


@pytest.mark.django_db
def test_note_edit_view_post_cannot_overwrite_other_users_note():
    """POSTing to the edit endpoint for a report where another user owns the
    note must NOT mutate that user's note nor steal its ownership.

    KNOWN BUG (security): because ``get_object`` selects the note by report
    only, ``form_valid`` rebinds ``owner`` to the requesting user and saves,
    overwriting the original owner's text and hijacking the row.
    """
    owner = UserFactory.create(is_active=True)
    attacker = UserFactory.create(is_active=True)
    report = create_test_report()
    victim_note = NoteFactory.create(owner=owner, report=report, text="Original")

    client = Client()
    client.force_login(attacker)

    response = client.post(
        reverse("note_edit", args=[report.pk]),
        {"text": "Hijacked"},
        HTTP_HX_REQUEST="true",
    )
    assert response.status_code == 204

    victim_note.refresh_from_db()
    # The victim's note must be untouched and still owned by them.
    assert victim_note.owner_id == owner.pk, "attacker took ownership of another user's note"
    assert victim_note.text == "Original", "attacker overwrote another user's note text"


@pytest.mark.django_db
def test_note_edit_view_post_creates_separate_note_per_user():
    """Two users each editing their note on the same report should end up with
    two distinct notes (the model allows one note per (owner, report)).

    KNOWN BUG: the second user's POST loads the first user's note (report-only
    lookup) and updates it in place instead of creating a second note, so only
    one Note row exists afterwards.
    """
    user_a = UserFactory.create(is_active=True)
    user_b = UserFactory.create(is_active=True)
    report = create_test_report()
    NoteFactory.create(owner=user_a, report=report, text="A's note")

    client = Client()
    client.force_login(user_b)
    response = client.post(
        reverse("note_edit", args=[report.pk]),
        {"text": "B's note"},
        HTTP_HX_REQUEST="true",
    )
    assert response.status_code == 204

    assert Note.objects.filter(report=report).count() == 2
    assert Note.objects.filter(owner=user_a, report=report, text="A's note").exists()
    assert Note.objects.filter(owner=user_b, report=report, text="B's note").exists()


@pytest.mark.django_db
def test_note_edit_view_post_empty_text_does_not_delete_other_users_note():
    """An attacker submitting empty text must not delete another user's note.

    KNOWN BUG: ``get_object`` resolves to the foreign note (which has a pk), and
    ``form_valid`` then deletes it because the submitted text is empty.
    """
    owner = UserFactory.create(is_active=True)
    attacker = UserFactory.create(is_active=True)
    report = create_test_report()
    NoteFactory.create(owner=owner, report=report, text="Keep me")

    client = Client()
    client.force_login(attacker)
    response = client.post(
        reverse("note_edit", args=[report.pk]),
        {"text": ""},
        HTTP_HX_REQUEST="true",
    )
    assert response.status_code == 204

    assert Note.objects.filter(owner=owner, report=report, text="Keep me").exists(), (
        "attacker deleted another user's note with empty text"
    )


@pytest.mark.django_db
def test_note_edit_view_post_update_own_note_when_other_user_has_one():
    """Sanity check: when the current user already has their own note on the
    report, editing must update THEIR note (not the other user's)."""
    user = UserFactory.create(is_active=True)
    other_user = UserFactory.create(is_active=True)
    report = create_test_report()
    own_note = NoteFactory.create(owner=user, report=report, text="Mine v1")
    other_note = NoteFactory.create(owner=other_user, report=report, text="Theirs")

    client = Client()
    client.force_login(user)
    response = client.post(
        reverse("note_edit", args=[report.pk]),
        {"text": "Mine v2"},
        HTTP_HX_REQUEST="true",
    )
    assert response.status_code == 204

    own_note.refresh_from_db()
    other_note.refresh_from_db()
    assert own_note.text == "Mine v2"
    assert other_note.text == "Theirs"
