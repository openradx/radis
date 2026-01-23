import json
from datetime import date, datetime

import pytest
from adit_radis_shared.accounts.factories import GroupFactory, UserFactory
from adit_radis_shared.token_authentication.models import Token
from django.test import Client
from django.utils import timezone

from radis.reports.api.viewsets import _bulk_upsert_reports
from radis.reports.models import Language, Metadata, Modality, Report


@pytest.mark.django_db
def test_bulk_upsert_creates_and_updates_reports(client: Client):
    user = UserFactory.create(is_active=True, is_staff=True)
    group = GroupFactory.create()
    _, token = Token.objects.create_token(user, "bulk upsert test", None)
    payload = [
        {
            "document_id": "DOC-1",
            "language": "en",
            "groups": [group.pk],
            "pacs_aet": "PACS",
            "pacs_name": "Test PACS",
            "pacs_link": "",
            "patient_id": "P1",
            "patient_birth_date": "1980-01-01",
            "patient_sex": "M",
            "study_description": "Study 1",
            "study_datetime": timezone.make_aware(datetime(2024, 1, 1, 0, 0, 0)),
            "study_instance_uid": "1.2.3.4",
            "accession_number": "ACC1",
            "modalities": ["CT"],
            "metadata": {"ris_filename": "file1"},
            "body": "Report body 1",
        },
        {
            "document_id": "DOC-2",
            "language": "de",
            "groups": [group.pk],
            "pacs_aet": "PACS",
            "pacs_name": "Test PACS",
            "pacs_link": "",
            "patient_id": "P2",
            "patient_birth_date": "1975-05-05",
            "patient_sex": "F",
            "study_description": "Study 2",
            "study_datetime": "2024-01-02T00:00:00Z",
            "study_instance_uid": "2.3.4.5",
            "accession_number": "ACC2",
            "modalities": ["MR"],
            "metadata": {"ris_filename": "file2"},
            "body": "Report body 2",
        },
    ]

    response = client.post(
        "/api/reports/bulk-upsert/",
        data=json.dumps(payload),
        content_type="application/json",
        headers={"Authorization": f"Token {token}"},
    )
    assert response.status_code == 200
    assert response.json() == {"created": 2, "updated": 0, "invalid": 0}

    assert Report.objects.count() == 2
    assert Language.objects.filter(code="en").exists()
    assert Language.objects.filter(code="de").exists()
    assert Modality.objects.filter(code="CT").exists()
    assert Modality.objects.filter(code="MR").exists()

    payload[0]["body"] = "Updated body"
    payload[0]["metadata"] = {"ris_filename": "file1", "extra": "value"}

    response = client.post(
        "/api/reports/bulk-upsert/",
        data=json.dumps(payload),
        content_type="application/json",
        headers={"Authorization": f"Token {token}"},
    )
    assert response.status_code == 200
    assert response.json() == {"created": 0, "updated": 2, "invalid": 0}

    report = Report.objects.get(document_id="DOC-1")
    assert report.body == "Updated body"
    assert Metadata.objects.filter(report=report).count() == 2


@pytest.mark.django_db
def test_bulk_upsert_dedupes_payload_entries(client: Client):
    user = UserFactory.create(is_active=True, is_staff=True)
    group = GroupFactory.create()
    _, token = Token.objects.create_token(user, "bulk upsert dedupe test", None)

    payload = [
        {
            "document_id": "DOC-1",
            "language": "en",
            "groups": [group.pk, group.pk],
            "pacs_aet": "PACS",
            "pacs_name": "Test PACS",
            "pacs_link": "",
            "patient_id": "P1",
            "patient_birth_date": "1980-01-01",
            "patient_sex": "M",
            "study_description": "Study 1",
            "study_datetime": "2024-01-01T00:00:00Z",
            "study_instance_uid": "1.2.3.4",
            "accession_number": "ACC1",
            "modalities": ["CT", "CT"],
            "metadata": {"ris_filename": "file1", "extra": "value"},
            "body": "First version",
        },
        {
            "document_id": "DOC-1",
            "language": "en",
            "groups": [group.pk],
            "pacs_aet": "PACS",
            "pacs_name": "Test PACS",
            "pacs_link": "",
            "patient_id": "P1",
            "patient_birth_date": "1980-01-01",
            "patient_sex": "M",
            "study_description": "Study 1",
            "study_datetime": "2024-01-01T00:00:00Z",
            "study_instance_uid": "1.2.3.4",
            "accession_number": "ACC1",
            "modalities": ["CT"],
            "metadata": {"ris_filename": "file2", "extra": "value"},
            "body": "Second version",
        },
    ]

    response = client.post(
        "/api/reports/bulk-upsert/",
        data=json.dumps(payload),
        content_type="application/json",
        headers={"Authorization": f"Token {token}"},
    )
    assert response.status_code == 200
    assert response.json() == {"created": 1, "updated": 0, "invalid": 0}

    report = Report.objects.get(document_id="DOC-1")
    assert report.body == "Second version"
    assert report.modalities.count() == 1
    assert report.groups.count() == 1
    assert Metadata.objects.filter(report=report).count() == 2


@pytest.mark.django_db
def test_bulk_upsert_dedupes_metadata_keys():
    group = GroupFactory.create()

    validated_reports = [
        {
            "document_id": "DOC-1",
            "language": {"code": "en"},
            "groups": [group],
            "pacs_aet": "PACS",
            "pacs_name": "Test PACS",
            "pacs_link": "",
            "patient_id": "P1",
            "patient_birth_date": date(1980, 1, 1),
            "patient_sex": "M",
            "study_description": "Study 1",
            "study_datetime": "2024-01-01T00:00:00Z",
            "study_instance_uid": "1.2.3.4",
            "accession_number": "ACC1",
            "modalities": [{"code": "CT"}],
            "metadata": [
                {"key": "ris_filename", "value": "file1"},
                {"key": "ris_filename", "value": "file2"},
            ],
            "body": "Report body 1",
        },
    ]

    created_ids, updated_ids = _bulk_upsert_reports(validated_reports)
    assert created_ids == ["DOC-1"]
    assert updated_ids == []

    report = Report.objects.get(document_id="DOC-1")
    metadata = Metadata.objects.get(report=report, key="ris_filename")
    assert metadata.value == "file2"
