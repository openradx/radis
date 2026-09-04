"""The projection duplicates access-control data, so operators need a way to
prove it still matches its sources -- after a restore, a bulk import, or a
Language.code rename, which no trigger covers."""

from io import StringIO

import pytest
from adit_radis_shared.accounts.factories import GroupFactory
from django.core.management import call_command
from django.core.management.base import CommandError

from radis.pgsearch.models import ReportSearchIndex
from radis.reports.factories import LanguageFactory, ReportFactory

pytestmark = pytest.mark.django_db


def test_reports_no_drift_on_a_healthy_corpus():
    report = ReportFactory.create(language=LanguageFactory.create(code="en"))
    report.groups.add(GroupFactory.create())

    out = StringIO()
    call_command("check_search_projection", stdout=out)

    assert "Reports without a search index row: 0" in out.getvalue()


def test_reports_how_many_reports_have_no_index_row_yet():
    """Informational, not an error: indexing is deferred, so a report can be
    legitimately waiting for its index row. The number is what lets an operator
    tell "200 pending" from "40,000 permanently stuck"."""
    report = ReportFactory.create(language=LanguageFactory.create(code="en"))
    report.groups.add(GroupFactory.create())
    ReportSearchIndex.objects.filter(report=report).delete()

    out = StringIO()
    call_command("check_search_projection", stdout=out)

    assert "Reports without a search index row: 1" in out.getvalue()
    assert "matches its sources" in out.getvalue()


def test_detects_drifted_group_ids():
    report = ReportFactory.create(language=LanguageFactory.create(code="en"))
    report.groups.add(GroupFactory.create())

    ReportSearchIndex.objects.filter(report=report).update(group_ids=[9999])

    with pytest.raises(CommandError, match="group_ids"):
        call_command("check_search_projection")
