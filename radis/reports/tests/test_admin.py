import re
from datetime import timedelta

import pytest
from adit_radis_shared.accounts.factories import UserFactory
from django.contrib.admin import helpers
from django.contrib.auth.models import Permission
from django.urls import reverse
from django.utils import timezone

from radis.pgsearch.models import ReportSearchIndex
from radis.reports import admin as reports_admin
from radis.reports.factories import LanguageFactory, ReportFactory
from radis.reports.models import Report

pytestmark = pytest.mark.django_db

CHANGELIST_URL = reverse("admin:reports_report_changelist")


def create_report() -> Report:
    return ReportFactory.create(language=LanguageFactory.create(code="en"))


def withdraw(report: Report, by=None, reason: str = "test reason") -> None:
    Report.objects.filter(pk=report.pk).update(
        withdrawn_at=timezone.now(), withdrawn_by=by, withdrawal_reason=reason
    )


def post_action(client, action: str, reports: list[Report], **extra):
    return client.post(
        CHANGELIST_URL,
        {"action": action, helpers.ACTION_CHECKBOX_NAME: [str(r.pk) for r in reports], **extra},
        follow=True,
    )


# ---------------------------------------------------------------------------
# Withdraw
# ---------------------------------------------------------------------------


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


def test_withdraw_action_sets_state_and_syncs_projection(admin_client, admin_user):
    report = create_report()
    updated_at = report.updated_at

    response = post_action(
        admin_client, "withdraw_selected", [report], apply="1", reason="wrong patient"
    )

    assert response.status_code == 200
    assert "Withdrew 1 report(s)." in response.content.decode()
    report.refresh_from_db()
    assert report.is_withdrawn
    assert report.withdrawn_by == admin_user
    assert report.withdrawal_reason == "wrong patient"
    # Withdrawing is not a content update: labels stay fresh.
    assert report.updated_at == updated_at
    assert ReportSearchIndex.objects.get(report=report).withdrawn is True


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

    response = post_action(
        admin_client, "withdraw_selected", [already, fresh], apply="1", reason="new reason"
    )

    assert "Withdrew 1 report(s)." in response.content.decode()
    already.refresh_from_db()
    fresh.refresh_from_db()
    assert already.withdrawal_reason == "original reason"
    assert already.withdrawn_at == original_time
    assert fresh.withdrawal_reason == "new reason"


def test_withdraw_action_warns_when_every_selected_report_is_withdrawn(admin_client):
    report = create_report()
    withdraw(report, reason="original reason")

    response = post_action(admin_client, "withdraw_selected", [report])

    content = response.content.decode()
    assert "already withdrawn" in content
    assert "Reason for withdrawal" not in content
    report.refresh_from_db()
    assert report.withdrawal_reason == "original reason"


def test_withdraw_action_refuses_a_selection_over_the_limit(admin_client, monkeypatch):
    monkeypatch.setattr(reports_admin, "WITHDRAW_MAX_REPORTS", 1)
    reports = [create_report(), create_report()]

    response = post_action(admin_client, "withdraw_selected", reports)

    content = response.content.decode()
    assert "withdraw at most 1 at a time" in content
    assert "Reason for withdrawal" not in content
    assert not Report.objects.filter(withdrawn_at__isnull=False).exists()


def test_confirmation_page_posts_back_what_the_action_needs(admin_client):
    """Submits the confirmation form as rendered, so a hidden input missing from
    the template fails here rather than only in the browser."""
    reports = [create_report(), create_report()]
    confirmation = admin_client.post(
        CHANGELIST_URL,
        {
            "action": "withdraw_selected",
            helpers.ACTION_CHECKBOX_NAME: [str(r.pk) for r in reports],
        },
    )
    html = confirmation.content.decode()
    form_html = html[html.index('<form method="post">') :]
    hidden: dict[str, list[str]] = {}
    for name, value in re.findall(r'type="hidden"\s+name="([^"]+)"\s+value="([^"]*)"', form_html):
        hidden.setdefault(name, []).append(value)

    response = admin_client.post(
        CHANGELIST_URL, {**hidden, "reason": "imported twice"}, follow=True
    )

    assert sorted(hidden[helpers.ACTION_CHECKBOX_NAME]) == sorted(str(r.pk) for r in reports)
    assert "Withdrew 2 report(s)." in response.content.decode()
    assert Report.objects.filter(withdrawal_reason="imported twice").count() == 2


# ---------------------------------------------------------------------------
# Restore
# ---------------------------------------------------------------------------


def test_restore_action_clears_state_and_resyncs_projection(admin_client, admin_user):
    report = create_report()
    withdraw(report, by=admin_user)
    report.refresh_from_db()
    withdrawn_updated_at = report.updated_at

    response = post_action(admin_client, "restore_selected", [report])

    assert "Restored 1 report(s)." in response.content.decode()
    report.refresh_from_db()
    assert not report.is_withdrawn
    assert report.withdrawn_by is None
    assert report.withdrawal_reason == ""
    # Subscriptions select reports by updated_at, so a restore moves it.
    assert report.updated_at > withdrawn_updated_at
    index = ReportSearchIndex.objects.get(report=report)
    assert index.withdrawn is False
    assert index.report_updated_at == report.updated_at


def test_restore_action_leaves_live_reports_alone(admin_client):
    live = create_report()
    gone = create_report()
    withdraw(gone)

    response = post_action(admin_client, "restore_selected", [live, gone])

    assert "Restored 1 report(s)." in response.content.decode()
    assert Report.objects.get(pk=live.pk).updated_at == live.updated_at


def test_restore_action_warns_when_no_selected_report_is_withdrawn(admin_client):
    live = create_report()

    response = post_action(admin_client, "restore_selected", [live])

    assert "None of the selected reports is withdrawn." in response.content.decode()


# ---------------------------------------------------------------------------
# The changelist and the change form
# ---------------------------------------------------------------------------


def test_withdrawn_filter_lists_withdrawn_or_live_reports(admin_client):
    live = create_report()
    gone = create_report()
    withdraw(gone)

    withdrawn_page = admin_client.get(CHANGELIST_URL, {"withdrawn": "yes"}).content.decode()
    live_page = admin_client.get(CHANGELIST_URL, {"withdrawn": "no"}).content.decode()

    assert gone.document_id in withdrawn_page
    assert live.document_id not in withdrawn_page
    assert live.document_id in live_page
    assert gone.document_id not in live_page


def test_search_matches_the_exact_document_id(admin_client):
    wanted = create_report()
    other = create_report()

    exact = admin_client.get(CHANGELIST_URL, {"q": wanted.document_id}).content.decode()
    partial = admin_client.get(CHANGELIST_URL, {"q": wanted.document_id[:8]}).content.decode()

    assert wanted.document_id in exact
    assert other.document_id not in exact
    assert wanted.document_id not in partial


def test_change_form_shows_the_withdrawal_read_only(admin_client):
    report = create_report()
    withdraw(report, reason="duplicate import")

    response = admin_client.get(reverse("admin:reports_report_change", args=[report.pk]))

    content = response.content.decode()
    assert "duplicate import" in content
    assert 'name="withdrawal_reason"' not in content
    assert 'name="withdrawn_at_0"' not in content
    assert 'name="withdrawn_by"' not in content


def test_staff_without_change_permission_cannot_withdraw_or_restore(client):
    viewer = UserFactory.create(is_staff=True, is_active=True)
    viewer.user_permissions.add(Permission.objects.get(codename="view_report"))
    client.force_login(viewer)
    live = create_report()
    gone = create_report()
    withdraw(gone, reason="original reason")

    changelist = client.get(CHANGELIST_URL).content.decode()
    post_action(client, "withdraw_selected", [live], apply="1", reason="no permission")
    post_action(client, "restore_selected", [gone])

    assert "withdraw_selected" not in changelist
    assert "restore_selected" not in changelist
    assert not Report.objects.get(pk=live.pk).is_withdrawn
    assert Report.objects.get(pk=gone.pk).withdrawal_reason == "original reason"
