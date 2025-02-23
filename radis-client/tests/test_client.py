import pytest
from pytest_django.live_server_helper import LiveServer
from pytest_mock import MockerFixture
from radis_client.client import RadisClient
from radis_client.utils.testing_helpers import create_admin_with_group_and_token, create_report_data

from radis.reports.api.serializers import ReportSerializer


@pytest.mark.django_db
def test_report_data_valid():
    report_data = create_report_data()
    report = ReportSerializer(data=report_data.to_dict())

    assert report.is_valid()


@pytest.mark.django_db
def test_report_data_post(live_server: LiveServer, mocker: MockerFixture):
    # Make sure it won't try to save created reports to any full text search database
    # as those are not available during test
    mocker.patch("radis.reports.api.viewsets.reports_created_handlers", return_value=[])

    _, _, token = create_admin_with_group_and_token()
    client = RadisClient(live_server.url, token)
    report_data = create_report_data()

    response = client.create_report(report_data)

    assert response
