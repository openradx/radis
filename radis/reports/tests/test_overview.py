from datetime import datetime
from datetime import timezone as dt_timezone

import pytest
from adit_radis_shared.accounts.factories import GroupFactory, UserFactory
from django.core.cache import cache
from django.core.management import call_command
from django.test import Client
from django.utils import timezone

from radis.reports.factories import ReportFactory


@pytest.mark.django_db
def test_report_overview_counts_are_group_scoped(client: Client):
    cache.clear()
    group = GroupFactory.create()
    other_group = GroupFactory.create()
    user = UserFactory.create(is_active=True)
    user.groups.add(group)
    user.active_group = group
    user.save()

    now = timezone.now()
    last_year = now.year - 1
    prev_year = now.year - 2

    report_last_year = ReportFactory.create(
        study_datetime=datetime(last_year, 1, 1, tzinfo=dt_timezone.utc)
    )
    report_last_year.groups.add(group)

    report_prev_year = ReportFactory.create(
        study_datetime=datetime(prev_year, 6, 1, tzinfo=dt_timezone.utc)
    )
    report_prev_year.groups.add(group)

    report_other_group = ReportFactory.create(
        study_datetime=datetime(last_year, 3, 1, tzinfo=dt_timezone.utc)
    )
    report_other_group.groups.add(other_group)

    call_command("rebuild_report_overview_stats")

    client.force_login(user)
    response = client.get("/reports/overview/")

    assert response.status_code == 200
    assert response.context["total_count"] == 2
    assert response.context["last_year_count"] == 1
    assert response.context["prev_year_count"] == 1


@pytest.mark.django_db
def test_report_overview_requires_active_group(client: Client):
    user = UserFactory.create(is_active=True)
    client.force_login(user)

    response = client.get("/reports/overview/")
    assert response.status_code == 403
