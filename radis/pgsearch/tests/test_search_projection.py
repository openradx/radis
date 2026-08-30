"""Tests for the ReportSearchIndex search projection.

The projection mirrors the Report fields that search filters on, so the FTS
candidate query can stay single-table. group_ids is access-control data, so
its correctness has its own tests here.
"""

import pytest

from radis.pgsearch.models import ReportSearchIndex
from radis.reports.factories import LanguageFactory, ReportFactory

pytestmark = pytest.mark.django_db


def test_new_index_row_defaults_to_empty_arrays():
    report = ReportFactory.create(language=LanguageFactory.create(code="en"))
    index = ReportSearchIndex.objects.get(report=report)

    assert index.group_ids == []
    assert index.modality_codes == []
