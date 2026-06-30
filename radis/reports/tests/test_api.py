"""API contract tests for the reports app.

These exercise the public REST contract of ``ReportViewSet`` (create, update,
upsert-via-PUT, bulk-upsert, de-duplication, M2M/metadata handling and the
``IsAdminUser`` authorization boundary) using DRF's ``APIClient``.

The viewset registers handlers via ``transaction.on_commit`` that push reports
into the external full-text search database. Under ``@pytest.mark.django_db``
the surrounding transaction is rolled back and never committed, so those
handlers do not fire. We additionally clear the handler lists in an autouse
fixture to keep the tests independent of any site registration and safe even if
a test opts into ``transaction=True``.
"""

from datetime import UTC, date, datetime

import pytest
from adit_radis_shared.accounts.factories import AdminUserFactory, GroupFactory, UserFactory
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APIClient

from radis.reports.factories import LanguageFactory, ReportFactory
from radis.reports.models import Language, Modality, Report

LIST_URL = reverse("report-list")
BULK_UPSERT_URL = reverse("report-bulk-upsert")


def detail_url(document_id: str) -> str:
    return reverse("report-detail", args=[document_id])


def make_payload(document_id: str = "doc-0001", **overrides) -> dict:
    """A valid create/upsert payload matching the wire format the serializer expects.

    ``language`` is a string, ``metadata`` a flat dict, ``modalities`` a list of
    codes and ``groups`` a list of Group PKs (see radis_client testing helpers).
    """
    group = overrides.pop("group", None) or GroupFactory.create()
    payload = {
        "document_id": document_id,
        "language": "en",
        "groups": [group.pk],
        "pacs_aet": "synapse",
        "pacs_name": "Synapse",
        "pacs_link": "http://synapse.example/34343",
        "patient_id": "1234578",
        "patient_birth_date": date(1976, 5, 23).isoformat(),
        "patient_sex": "M",
        "study_description": "CT of the Thorax",
        "study_datetime": datetime(2000, 8, 10, 11, 37, tzinfo=UTC).isoformat(),
        "study_instance_uid": "34343-34343-34343",
        "accession_number": "345348389",
        "modalities": ["CT", "PT"],
        "metadata": {
            "series_instance_uid": "34343-676556-3343",
            "sop_instance_uid": "35858-384834-3843",
        },
        "body": "This is the report",
    }
    payload.update(overrides)
    return payload


@pytest.fixture(autouse=True)
def _no_report_handlers(monkeypatch):
    """Prevent on-commit handlers (full-text search sync) from running."""
    monkeypatch.setattr("radis.reports.api.viewsets.reports_created_handlers", [])
    monkeypatch.setattr("radis.reports.api.viewsets.reports_updated_handlers", [])
    monkeypatch.setattr("radis.reports.api.viewsets.reports_deleted_handlers", [])


@pytest.fixture
def admin_client() -> APIClient:
    client = APIClient()
    client.force_authenticate(user=AdminUserFactory.create())
    return client


@pytest.fixture
def user_client() -> APIClient:
    """Authenticated but non-admin (not staff)."""
    client = APIClient()
    client.force_authenticate(user=UserFactory.create(is_staff=False))
    return client


# --------------------------------------------------------------------------- #
# Create
# --------------------------------------------------------------------------- #


@pytest.mark.django_db
def test_create_report(admin_client):
    payload = make_payload(document_id="doc-create")

    response = admin_client.post(LIST_URL, payload, format="json")

    assert response.status_code == status.HTTP_201_CREATED
    report = Report.objects.get(document_id="doc-create")
    assert report.body == "This is the report"
    assert report.patient_id == "1234578"
    assert report.language.code == "en"


@pytest.mark.django_db
def test_create_report_creates_m2m_metadata_and_language(admin_client):
    payload = make_payload(document_id="doc-m2m")

    response = admin_client.post(LIST_URL, payload, format="json")

    assert response.status_code == status.HTTP_201_CREATED
    report = Report.objects.get(document_id="doc-m2m")

    # language + modalities are get_or_created
    assert Language.objects.filter(code="en").exists()
    assert sorted(report.modality_codes) == ["CT", "PT"]
    assert set(Modality.objects.values_list("code", flat=True)) >= {"CT", "PT"}

    # metadata dict -> Metadata rows
    metadata = {m.key: m.value for m in report.metadata.all()}
    assert metadata == {
        "series_instance_uid": "34343-676556-3343",
        "sop_instance_uid": "35858-384834-3843",
    }

    # groups M2M is set from PKs
    assert list(report.groups.values_list("pk", flat=True)) == payload["groups"]


@pytest.mark.django_db
def test_create_response_round_trips_representation(admin_client):
    """to_representation must collapse language/metadata/modalities back to the
    flat wire format (string / dict / list of codes)."""
    payload = make_payload(document_id="doc-repr")

    response = admin_client.post(LIST_URL, payload, format="json")

    assert response.status_code == status.HTTP_201_CREATED
    data = response.json()
    assert data["language"] == "en"
    assert data["metadata"] == payload["metadata"]
    assert sorted(data["modalities"]) == ["CT", "PT"]


@pytest.mark.django_db
def test_create_rejects_unknown_fields(admin_client):
    payload = make_payload(document_id="doc-unknown")
    payload["bogus_field"] = "nope"

    response = admin_client.post(LIST_URL, payload, format="json")

    assert response.status_code == status.HTTP_400_BAD_REQUEST


# --------------------------------------------------------------------------- #
# De-duplication by document_id
# --------------------------------------------------------------------------- #


@pytest.mark.django_db
def test_create_duplicate_document_id_rejected(admin_client):
    group = GroupFactory.create()
    payload = make_payload(document_id="doc-dup", group=group)

    first = admin_client.post(LIST_URL, payload, format="json")
    assert first.status_code == status.HTTP_201_CREATED

    second = admin_client.post(
        LIST_URL, make_payload(document_id="doc-dup", group=group), format="json"
    )
    assert second.status_code == status.HTTP_400_BAD_REQUEST
    assert Report.objects.filter(document_id="doc-dup").count() == 1


# --------------------------------------------------------------------------- #
# Update (PUT, no upsert)
# --------------------------------------------------------------------------- #


@pytest.mark.django_db
def test_update_existing_report(admin_client):
    LanguageFactory.create(code="en")
    group = GroupFactory.create()
    admin_client.post(LIST_URL, make_payload(document_id="doc-upd", group=group), format="json")

    updated = make_payload(document_id="doc-upd", group=group)
    updated["body"] = "Updated findings"
    updated["modalities"] = ["MR"]
    updated["metadata"] = {"new_key": "new_value"}

    response = admin_client.put(detail_url("doc-upd"), updated, format="json")

    assert response.status_code == status.HTTP_200_OK
    report = Report.objects.get(document_id="doc-upd")
    assert report.body == "Updated findings"
    assert report.modality_codes == ["MR"]
    # update() deletes old metadata then recreates
    assert {m.key: m.value for m in report.metadata.all()} == {"new_key": "new_value"}


@pytest.mark.django_db
def test_update_nonexistent_without_upsert_404(admin_client):
    LanguageFactory.create(code="en")
    response = admin_client.put(
        detail_url("does-not-exist"), make_payload(document_id="does-not-exist"), format="json"
    )
    assert response.status_code == status.HTTP_404_NOT_FOUND


@pytest.mark.django_db
def test_partial_update_not_allowed(admin_client):
    group = GroupFactory.create()
    admin_client.post(LIST_URL, make_payload(document_id="doc-patch", group=group), format="json")

    response = admin_client.patch(detail_url("doc-patch"), {"body": "x"}, format="json")

    assert response.status_code == status.HTTP_405_METHOD_NOT_ALLOWED


# --------------------------------------------------------------------------- #
# Upsert via PUT  (?upsert=true) -> 201 vs 200 semantics
# --------------------------------------------------------------------------- #


@pytest.mark.django_db
def test_upsert_put_creates_with_201(admin_client):
    payload = make_payload(document_id="doc-upsert-new")

    response = admin_client.put(
        detail_url("doc-upsert-new") + "?upsert=true", payload, format="json"
    )

    assert response.status_code == status.HTTP_201_CREATED
    assert Report.objects.filter(document_id="doc-upsert-new").exists()


@pytest.mark.django_db
def test_upsert_put_updates_existing_with_200(admin_client):
    group = GroupFactory.create()
    admin_client.post(
        LIST_URL, make_payload(document_id="doc-upsert-exist", group=group), format="json"
    )

    updated = make_payload(document_id="doc-upsert-exist", group=group)
    updated["body"] = "Upserted body"

    response = admin_client.put(
        detail_url("doc-upsert-exist") + "?upsert=true", updated, format="json"
    )

    assert response.status_code == status.HTTP_200_OK
    assert Report.objects.get(document_id="doc-upsert-exist").body == "Upserted body"
    assert Report.objects.filter(document_id="doc-upsert-exist").count() == 1


# --------------------------------------------------------------------------- #
# bulk_upsert
# --------------------------------------------------------------------------- #


@pytest.mark.django_db
def test_bulk_upsert_creates_and_updates(admin_client):
    group = GroupFactory.create()
    # Pre-existing report that the bulk call should update (not duplicate).
    admin_client.post(
        LIST_URL, make_payload(document_id="bulk-existing", group=group), format="json"
    )

    body = [
        make_payload(document_id="bulk-existing", group=group, body="updated via bulk"),
        make_payload(document_id="bulk-new-1", group=group),
        make_payload(document_id="bulk-new-2", group=group),
    ]

    response = admin_client.post(BULK_UPSERT_URL, body, format="json")

    assert response.status_code == status.HTTP_200_OK
    data = response.json()
    assert data["created"] == 2
    assert data["updated"] == 1
    assert data["invalid"] == 0

    assert Report.objects.count() == 3
    assert Report.objects.get(document_id="bulk-existing").body == "updated via bulk"


@pytest.mark.django_db
def test_bulk_upsert_persists_m2m_and_metadata(admin_client):
    group = GroupFactory.create()
    body = [make_payload(document_id="bulk-meta", group=group)]

    response = admin_client.post(BULK_UPSERT_URL, body, format="json")

    assert response.status_code == status.HTTP_200_OK
    report = Report.objects.get(document_id="bulk-meta")
    assert sorted(report.modality_codes) == ["CT", "PT"]
    assert {m.key: m.value for m in report.metadata.all()} == {
        "series_instance_uid": "34343-676556-3343",
        "sop_instance_uid": "35858-384834-3843",
    }
    assert list(report.groups.values_list("pk", flat=True)) == [group.pk]


@pytest.mark.django_db
def test_bulk_upsert_rewrites_metadata_and_modalities_on_update(admin_client):
    """A second bulk upsert of the same document_id replaces its metadata and
    modality rows rather than appending."""
    group = GroupFactory.create()
    admin_client.post(
        BULK_UPSERT_URL, [make_payload(document_id="bulk-rw", group=group)], format="json"
    )

    changed = make_payload(document_id="bulk-rw", group=group)
    changed["modalities"] = ["US"]
    changed["metadata"] = {"only_key": "only_value"}

    response = admin_client.post(BULK_UPSERT_URL, [changed], format="json")

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["updated"] == 1
    report = Report.objects.get(document_id="bulk-rw")
    assert report.modality_codes == ["US"]
    assert {m.key: m.value for m in report.metadata.all()} == {"only_key": "only_value"}


@pytest.mark.django_db
def test_bulk_upsert_reports_invalid_rows(admin_client):
    group = GroupFactory.create()
    valid = make_payload(document_id="bulk-ok", group=group)
    invalid = make_payload(document_id="bulk-bad", group=group)
    del invalid["body"]  # body is required

    response = admin_client.post(BULK_UPSERT_URL, [valid, invalid], format="json")

    assert response.status_code == status.HTTP_200_OK
    data = response.json()
    assert data["created"] == 1
    assert data["invalid"] == 1
    assert "errors" in data
    assert data["errors"][0]["document_id"] == "bulk-bad"
    assert Report.objects.filter(document_id="bulk-ok").exists()
    assert not Report.objects.filter(document_id="bulk-bad").exists()


@pytest.mark.django_db
def test_bulk_upsert_requires_list(admin_client):
    response = admin_client.post(
        BULK_UPSERT_URL, make_payload(document_id="not-a-list"), format="json"
    )
    assert response.status_code == status.HTTP_400_BAD_REQUEST


# --------------------------------------------------------------------------- #
# Authorization (IsAdminUser)
# --------------------------------------------------------------------------- #


@pytest.mark.django_db
def test_create_rejected_for_non_admin(user_client):
    response = user_client.post(LIST_URL, make_payload(document_id="doc-forbidden"), format="json")
    assert response.status_code == status.HTTP_403_FORBIDDEN
    assert not Report.objects.filter(document_id="doc-forbidden").exists()


@pytest.mark.django_db
def test_create_rejected_for_anonymous():
    client = APIClient()
    response = client.post(LIST_URL, make_payload(document_id="doc-anon"), format="json")
    assert response.status_code in (
        status.HTTP_401_UNAUTHORIZED,
        status.HTTP_403_FORBIDDEN,
    )
    assert not Report.objects.filter(document_id="doc-anon").exists()


@pytest.mark.django_db
def test_bulk_upsert_rejected_for_non_admin(user_client):
    group = GroupFactory.create()
    response = user_client.post(
        BULK_UPSERT_URL, [make_payload(document_id="bulk-forbidden", group=group)], format="json"
    )
    assert response.status_code == status.HTTP_403_FORBIDDEN
    assert not Report.objects.filter(document_id="bulk-forbidden").exists()


@pytest.mark.django_db
def test_upsert_put_create_rejected_for_non_admin(user_client):
    """An upsert PUT that would create a new report re-checks POST permission for
    the cloned request; a non-admin must be refused."""
    response = user_client.put(
        detail_url("doc-upsert-forbidden") + "?upsert=true",
        make_payload(document_id="doc-upsert-forbidden"),
        format="json",
    )
    assert response.status_code == status.HTTP_403_FORBIDDEN
    assert not Report.objects.filter(document_id="doc-upsert-forbidden").exists()


@pytest.mark.django_db
def test_update_rejected_for_non_admin(user_client):
    # Seed a report directly (bypassing the API) so the PUT targets an existing object.
    LanguageFactory.create(code="en")
    report = ReportFactory.create(document_id="doc-upd-forbidden", modalities=["CT"])

    response = user_client.put(
        detail_url("doc-upd-forbidden"),
        make_payload(document_id="doc-upd-forbidden"),
        format="json",
    )
    assert response.status_code == status.HTTP_403_FORBIDDEN
    report.refresh_from_db()
    assert report.body != "This is the report"
