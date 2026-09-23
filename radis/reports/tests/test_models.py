import pytest
from adit_radis_shared.accounts.factories import GroupFactory
from django.utils import timezone

from radis.reports.factories import LanguageFactory, ReportFactory
from radis.reports.models import Report

pytestmark = pytest.mark.django_db


def create_report() -> Report:
    return ReportFactory.create(language=LanguageFactory.create(code="en"))


def test_report_defaults_to_live():
    report = create_report()

    assert not report.is_withdrawn
    assert Report.objects.live().filter(pk=report.pk).exists()


def test_live_excludes_withdrawn_reports_but_default_manager_keeps_them():
    report = create_report()

    Report.objects.filter(pk=report.pk).update(withdrawn_at=timezone.now())

    report.refresh_from_db()
    assert report.is_withdrawn
    assert not Report.objects.live().filter(pk=report.pk).exists()
    assert Report.objects.filter(pk=report.pk).exists()


def test_live_is_available_on_related_managers():
    group = GroupFactory.create()
    live = create_report()
    withdrawn = create_report()
    live.groups.add(group)
    withdrawn.groups.add(group)

    Report.objects.filter(pk=withdrawn.pk).update(withdrawn_at=timezone.now())

    assert list(group.reports.live()) == [live]  # type: ignore[attr-defined]
