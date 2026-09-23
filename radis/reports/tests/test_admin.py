from datetime import timedelta

import pytest
from django.contrib.admin import helpers
from django.contrib.admin.models import CHANGE, LogEntry
from django.contrib.contenttypes.models import ContentType
from django.urls import reverse
from django.utils import timezone

from radis.pgsearch.models import ReportSearchIndex
from radis.reports.factories import LanguageFactory, ReportFactory
from radis.reports.models import Report

pytestmark = pytest.mark.django_db

CHANGELIST_URL = reverse("admin:reports_report_changelist")


def create_report() -> Report:
    return ReportFactory.create(language=LanguageFactory.create(code="en"))


def test_withdraw_action_asks_for_a_reason(admin_client):
    report = create_report()

    response = admin_client.post(
        CHANGELIST_URL,
        {"action": "withdraw_selected", helpers.ACTION_CHECKBOX_NAME: [str(report.pk)]},
    )

    assert response.status_code == 200
    assert b"Reason for withdrawal" in response.content
    report.refresh_from_db()
    assert not report.is_withdrawn


def test_withdraw_action_sets_state_logs_and_syncs_projection(admin_client):
    report = create_report()

    response = admin_client.post(
        CHANGELIST_URL,
        {
            "action": "withdraw_selected",
            helpers.ACTION_CHECKBOX_NAME: [str(report.pk)],
            "apply": "1",
            "reason": "wrong patient",
        },
        follow=True,
    )

    assert response.status_code == 200
    report.refresh_from_db()
    assert report.is_withdrawn
    assert report.withdrawn_by is not None
    assert report.withdrawal_reason == "wrong patient"
    assert ReportSearchIndex.objects.get(report=report).withdrawn is True
    log = LogEntry.objects.get(object_id=str(report.pk), action_flag=CHANGE)
    assert "wrong patient" in log.change_message


def test_withdraw_action_requires_a_reason(admin_client):
    report = create_report()

    response = admin_client.post(
        CHANGELIST_URL,
        {
            "action": "withdraw_selected",
            helpers.ACTION_CHECKBOX_NAME: [str(report.pk)],
            "apply": "1",
            "reason": "",
        },
    )

    assert response.status_code == 200
    report.refresh_from_db()
    assert not report.is_withdrawn


def test_withdraw_action_skips_already_withdrawn_reports(admin_client):
    already = create_report()
    original_time = timezone.now() - timedelta(days=1)
    Report.objects.filter(pk=already.pk).update(
        withdrawn_at=original_time, withdrawal_reason="original reason"
    )
    fresh = create_report()

    admin_client.post(
        CHANGELIST_URL,
        {
            "action": "withdraw_selected",
            helpers.ACTION_CHECKBOX_NAME: [str(already.pk), str(fresh.pk)],
            "apply": "1",
            "reason": "new reason",
        },
    )

    already.refresh_from_db()
    fresh.refresh_from_db()
    assert already.withdrawal_reason == "original reason"
    assert already.withdrawn_at == original_time
    assert fresh.withdrawal_reason == "new reason"


WITHDRAWN_CHANGELIST_URL = reverse("admin:reports_withdrawnreport_changelist")


def withdraw(report: Report) -> None:
    Report.objects.filter(pk=report.pk).update(
        withdrawn_at=timezone.now(), withdrawal_reason="test reason"
    )


def test_withdrawn_listing_shows_only_withdrawn_reports(admin_client):
    live = create_report()
    gone = create_report()
    withdraw(gone)

    response = admin_client.get(WITHDRAWN_CHANGELIST_URL)

    content = response.content.decode()
    assert gone.document_id in content
    assert live.document_id not in content


def test_withdrawn_listing_has_no_add_button(admin_client):
    response = admin_client.get(reverse("admin:reports_withdrawnreport_add"))

    assert response.status_code == 403


def test_restore_action_clears_state_logs_and_resyncs_projection(admin_client):
    report = create_report()
    withdraw(report)

    response = admin_client.post(
        WITHDRAWN_CHANGELIST_URL,
        {"action": "restore_selected", helpers.ACTION_CHECKBOX_NAME: [str(report.pk)]},
        follow=True,
    )

    assert response.status_code == 200
    report.refresh_from_db()
    assert not report.is_withdrawn
    assert report.withdrawn_by is None
    assert report.withdrawal_reason == ""
    assert ReportSearchIndex.objects.get(report=report).withdrawn is False
    logs = LogEntry.objects.filter(
        object_id=str(report.pk),
        action_flag=CHANGE,
        content_type=ContentType.objects.get_for_model(Report),
    )
    assert any("Restored" in entry.change_message for entry in logs)
