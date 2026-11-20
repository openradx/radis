import pytest
from adit_radis_shared.accounts.factories import GroupFactory, UserFactory
from adit_radis_shared.common.utils.testing_helpers import add_user_to_group
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from radis.reports.factories import LanguageFactory, ReportFactory
from radis.reports.models import Report

pytestmark = pytest.mark.django_db


def login_with_active_group(client: Client):
    user = UserFactory.create(is_active=True)
    group = GroupFactory.create()
    add_user_to_group(user, group)
    client.force_login(user)
    return user, group


def create_report_for(group) -> Report:
    report = ReportFactory.create(language=LanguageFactory.create(code="en"))
    report.groups.add(group)
    return report


def test_report_detail_view_shows_live_report(client: Client):
    _, group = login_with_active_group(client)
    report = create_report_for(group)

    response = client.get(reverse("report_detail", args=[report.pk]))

    assert response.status_code == 200


def test_report_detail_view_404s_for_withdrawn_report(client: Client):
    _, group = login_with_active_group(client)
    report = create_report_for(group)
    Report.objects.filter(pk=report.pk).update(withdrawn_at=timezone.now())

    response = client.get(reverse("report_detail", args=[report.pk]))

    assert response.status_code == 404


def test_report_body_view_404s_for_withdrawn_report(client: Client):
    _, group = login_with_active_group(client)
    report = create_report_for(group)
    Report.objects.filter(pk=report.pk).update(withdrawn_at=timezone.now())

    response = client.get(reverse("report_body", args=[report.pk]))

    assert response.status_code == 404


def test_report_list_view_hides_withdrawn_reports(client: Client):
    _, group = login_with_active_group(client)
    live = create_report_for(group)
    withdrawn = create_report_for(group)
    Report.objects.filter(pk=withdrawn.pk).update(withdrawn_at=timezone.now())

    response = client.get(reverse("report_list"))

    assert response.status_code == 200
    reports = list(response.context["reports"])
    assert live in reports
    assert withdrawn not in reports
