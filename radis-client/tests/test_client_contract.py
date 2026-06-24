"""Contract tests for ``radis_client.RadisClient`` against the live API.

The repo ships only two thin client tests. These pin the request/response
*shapes* of every public ``RadisClient`` method against a real RADIS server
(``live_server``) so that a wire-format change on either side (client
``to_dict`` serialization or DRF ``ReportSerializer`` representation) is caught.

Run with the test settings -- under ``radis.settings.development`` the DRF
browsable-API renderer pulls in the debug toolbar and the live server 500s on a
missing ``djdt`` namespace::

    DJANGO_SETTINGS_MODULE=radis.settings.test uv run pytest \
        radis-client/tests/test_client_contract.py

The on-commit handlers that push reports into the external full-text search DB
are stubbed out (that DB is not available in tests).
"""

from datetime import date, datetime

import pytest
import requests
from pytest_django.live_server_helper import LiveServer
from pytest_mock import MockerFixture
from radis_client.client import RadisClient, ReportData
from radis_client.utils.testing_helpers import (
    create_admin_with_group_and_token,
    create_report_data,
)

from radis.reports.models import Group, Report


@pytest.fixture(autouse=True)
def _stub_search_handlers(mocker: MockerFixture):
    """Created/updated/deleted reports must not be pushed to an external FTS DB."""
    mocker.patch("radis.reports.api.viewsets.reports_created_handlers", [])
    mocker.patch("radis.reports.api.viewsets.reports_updated_handlers", [])
    mocker.patch("radis.reports.api.viewsets.reports_deleted_handlers", [])


@pytest.fixture
def client(live_server: LiveServer) -> RadisClient:
    _user, _group, token = create_admin_with_group_and_token()
    return RadisClient(live_server.url, token)


# --------------------------------------------------------------------------- #
# ReportData.to_dict serialization contract
# --------------------------------------------------------------------------- #


def test_report_data_to_dict_serializes_dates_and_keeps_shapes():
    """``to_dict`` must ISO-format the date/datetime fields and leave the nested
    collections in the flat wire shape the API expects."""
    rd = ReportData(
        document_id="C-1",
        language="en",
        groups=[7],
        pacs_aet="synapse",
        pacs_name="Synapse",
        pacs_link="http://x/1",
        patient_id="1",
        patient_birth_date=date(1976, 5, 23),
        patient_sex="M",
        study_description="CT Thorax",
        study_datetime=datetime(2000, 8, 10, 11, 37),
        study_instance_uid="1.2.3",
        accession_number="42",
        modalities=["CT", "PET"],
        metadata={"a": "b"},
        body="report",
    )

    data = rd.to_dict()

    assert data["patient_birth_date"] == "1976-05-23"
    assert data["study_datetime"] == "2000-08-10T11:37:00"
    assert data["language"] == "en"  # plain string, not nested
    assert data["modalities"] == ["CT", "PET"]  # list of codes
    assert data["metadata"] == {"a": "b"}  # flat dict
    assert data["groups"] == [7]


# --------------------------------------------------------------------------- #
# create_report
# --------------------------------------------------------------------------- #


@pytest.mark.django_db
def test_create_report_response_contract(client: RadisClient):
    report_data = create_report_data()

    response = client.create_report(report_data)

    # Response is the flat wire representation, not the nested input shape.
    assert response["document_id"] == report_data.document_id
    assert response["language"] == "en"
    assert response["metadata"] == report_data.metadata
    assert sorted(response["modalities"]) == sorted(report_data.modalities)
    assert response["groups"] == report_data.groups
    assert response["body"] == report_data.body
    # Server-derived/managed fields are present.
    assert "id" in response
    assert "patient_age" in response
    assert "created_at" in response and "updated_at" in response
    # patient_birth_date round-trips as an ISO date string.
    assert response["patient_birth_date"] == "1976-05-23"

    # And the report really exists server-side.
    assert Report.objects.filter(document_id=report_data.document_id).exists()


# --------------------------------------------------------------------------- #
# retrieve_report
# --------------------------------------------------------------------------- #


@pytest.mark.django_db
def test_retrieve_report_round_trips(client: RadisClient):
    report_data = create_report_data()
    client.create_report(report_data)

    retrieved = client.retrieve_report(report_data.document_id)

    assert retrieved["document_id"] == report_data.document_id
    assert retrieved["language"] == "en"
    assert retrieved["metadata"] == report_data.metadata
    assert sorted(retrieved["modalities"]) == sorted(report_data.modalities)


@pytest.mark.django_db
def test_retrieve_report_full_includes_documents_key(client: RadisClient):
    """``full=True`` triggers the document-fetcher branch and adds a
    ``documents`` mapping (empty when no fetchers are registered)."""
    report_data = create_report_data()
    client.create_report(report_data)

    retrieved = client.retrieve_report(report_data.document_id, full=True)

    assert "documents" in retrieved
    assert isinstance(retrieved["documents"], dict)


@pytest.mark.django_db
def test_retrieve_missing_report_raises_http_error(client: RadisClient):
    with pytest.raises(requests.HTTPError) as exc:
        client.retrieve_report("does-not-exist")
    assert exc.value.response is not None
    assert exc.value.response.status_code == 404


# --------------------------------------------------------------------------- #
# update_report  (PUT, with and without upsert)
# --------------------------------------------------------------------------- #


@pytest.mark.django_db
def test_update_existing_report_returns_updated_body(client: RadisClient):
    report_data = create_report_data()
    client.create_report(report_data)

    report_data.body = "Updated findings via client"
    response = client.update_report(report_data.document_id, report_data)

    assert response["body"] == "Updated findings via client"
    assert response["document_id"] == report_data.document_id
    assert Report.objects.get(document_id=report_data.document_id).body == (
        "Updated findings via client"
    )


@pytest.mark.django_db
def test_update_missing_without_upsert_raises_404(client: RadisClient):
    report_data = create_report_data()  # never created server-side
    with pytest.raises(requests.HTTPError) as exc:
        client.update_report(report_data.document_id, report_data, upsert=False)
    assert exc.value.response.status_code == 404


@pytest.mark.django_db
def test_update_with_upsert_creates_missing_report(client: RadisClient):
    report_data = create_report_data()

    response = client.update_report(report_data.document_id, report_data, upsert=True)

    # Upsert-create returns the created representation.
    assert response["document_id"] == report_data.document_id
    assert Report.objects.filter(document_id=report_data.document_id).exists()


# --------------------------------------------------------------------------- #
# update_reports_bulk
# --------------------------------------------------------------------------- #


def _bulk_report_data(document_id: str, group: Group) -> ReportData:
    """A bulk ReportData reusing one group (``create_report_data`` makes a fresh
    uniquely-named group on every call, which collides if used more than once)."""
    return ReportData(
        document_id=document_id,
        language="en",
        groups=[group.pk],
        pacs_aet="synapse",
        pacs_name="Synapse",
        pacs_link="http://synapse.net/1",
        patient_id="1234578",
        patient_birth_date=date(1976, 5, 23),
        patient_sex="M",
        study_description="CT of the Thorax",
        study_datetime=datetime(2000, 8, 10, 11, 37),
        study_instance_uid="1.2.3",
        accession_number="345348389",
        modalities=["CT", "PET"],
        metadata={"k": "v"},
        body="This is the report",
    )


@pytest.mark.django_db
def test_bulk_upsert_response_contract(client: RadisClient):
    group = Group.objects.create(name="BulkGroup")

    response = client.update_reports_bulk(
        [
            _bulk_report_data("bulk-c-1", group),
            _bulk_report_data("bulk-c-2", group),
        ]
    )

    # The bulk endpoint returns counters, not the report bodies.
    assert response["created"] == 2
    assert response["updated"] == 0
    assert response["invalid"] == 0
    assert Report.objects.filter(document_id__in=["bulk-c-1", "bulk-c-2"]).count() == 2


@pytest.mark.django_db
def test_bulk_upsert_updates_existing(client: RadisClient):
    group = Group.objects.create(name="BulkGroup2")
    first = _bulk_report_data("bulk-upd", group)
    client.create_report(first)

    first.body = "second pass body"
    response = client.update_reports_bulk([first])

    assert response["created"] == 0
    assert response["updated"] == 1
    assert Report.objects.get(document_id="bulk-upd").body == "second pass body"


# --------------------------------------------------------------------------- #
# delete_report
# --------------------------------------------------------------------------- #


@pytest.mark.django_db
def test_delete_report_returns_none_and_removes(client: RadisClient):
    report_data = create_report_data()
    client.create_report(report_data)

    result = client.delete_report(report_data.document_id)

    assert result is None
    assert not Report.objects.filter(document_id=report_data.document_id).exists()


@pytest.mark.django_db
def test_delete_missing_report_raises_404(client: RadisClient):
    with pytest.raises(requests.HTTPError) as exc:
        client.delete_report("never-existed")
    assert exc.value.response.status_code == 404


# --------------------------------------------------------------------------- #
# Auth contract
# --------------------------------------------------------------------------- #


@pytest.mark.django_db
def test_bad_token_is_rejected(live_server: LiveServer):
    bad_client = RadisClient(live_server.url, "not-a-real-token")
    report_data = create_report_data()

    with pytest.raises(requests.HTTPError) as exc:
        bad_client.create_report(report_data)
    assert exc.value.response.status_code in (401, 403)
