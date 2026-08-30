"""Tests for the ReportSearchIndex search projection.

The projection mirrors the Report fields that search filters on, so the FTS
candidate query can stay single-table. group_ids is access-control data, so
its correctness has its own tests here.
"""

import pytest
from adit_radis_shared.accounts.factories import GroupFactory
from django.db import connection

from radis.pgsearch.models import ReportSearchIndex
from radis.pgsearch.utils.indexing import bulk_upsert_report_search_indexes
from radis.reports.factories import LanguageFactory, ModalityFactory, ReportFactory
from radis.reports.models import Report

pytestmark = pytest.mark.django_db


def test_new_index_row_defaults_to_empty_arrays():
    # modalities=[] is explicit: ReportFactory attaches a random non-empty set
    # by default, and the modality_codes trigger added in this module would
    # otherwise correctly mirror it, defeating the point of this test.
    report = ReportFactory.create(language=LanguageFactory.create(code="en"), modalities=[])
    index = ReportSearchIndex.objects.get(report=report)

    assert index.group_ids == []
    assert index.modality_codes == []


def test_adding_a_group_updates_the_projection():
    report = ReportFactory.create(language=LanguageFactory.create(code="en"))
    group = GroupFactory.create()

    report.groups.add(group)

    index = ReportSearchIndex.objects.get(report=report)
    assert index.group_ids == [group.pk]


def test_removing_a_group_updates_the_projection():
    """The leak direction: a report removed from a group must stop being visible."""
    report = ReportFactory.create(language=LanguageFactory.create(code="en"))
    group = GroupFactory.create()
    report.groups.add(group)

    report.groups.remove(group)

    index = ReportSearchIndex.objects.get(report=report)
    assert index.group_ids == []


def test_adding_a_modality_updates_the_projection():
    report = ReportFactory.create(language=LanguageFactory.create(code="en"), modalities=[])
    modality = ModalityFactory.create(code="CT")

    report.modalities.add(modality)

    index = ReportSearchIndex.objects.get(report=report)
    assert index.modality_codes == ["CT"]


def test_removing_a_modality_updates_the_projection():
    report = ReportFactory.create(language=LanguageFactory.create(code="en"), modalities=[])
    modality = ModalityFactory.create(code="CT")
    report.modalities.add(modality)

    report.modalities.remove(modality)

    index = ReportSearchIndex.objects.get(report=report)
    assert index.modality_codes == []


def test_raw_sql_membership_write_updates_the_projection():
    """The whole reason for triggers over m2m_changed: writers that bypass the ORM."""
    report = ReportFactory.create(language=LanguageFactory.create(code="en"))
    group = GroupFactory.create()

    with connection.cursor() as cursor:
        cursor.execute(
            "INSERT INTO reports_report_groups (report_id, group_id) VALUES (%s, %s)",
            [report.pk, group.pk],
        )

    index = ReportSearchIndex.objects.get(report=report)
    assert index.group_ids == [group.pk]


def test_bulk_membership_insert_updates_every_affected_row():
    """Statement-level triggers fail classically by processing only one transition row.

    This is the shape reports/api/viewsets.py:229 uses for bulk upsert.
    """
    language = LanguageFactory.create(code="en")
    reports = [ReportFactory.create(language=language) for _ in range(5)]
    group = GroupFactory.create()

    through = Report.groups.through
    through.objects.bulk_create(
        [through(report_id=report.pk, group_id=group.pk) for report in reports]
    )

    for report in reports:
        index = ReportSearchIndex.objects.get(report=report)
        assert index.group_ids == [group.pk], f"report {report.pk} was not updated"


def test_deleting_a_report_does_not_error():
    """Deleting a Report cascades to both the membership rows and the index row,
    firing the membership trigger against a row that may already be gone. The
    spec calls that harmless in either cascade order; this pins it."""
    report = ReportFactory.create(language=LanguageFactory.create(code="en"))
    report.groups.add(GroupFactory.create())
    report_pk = report.pk

    report.delete()

    assert not ReportSearchIndex.objects.filter(report_id=report_pk).exists()


def test_updating_a_report_updates_the_mirrored_scalars():
    report = ReportFactory.create(language=LanguageFactory.create(code="en"))

    report.patient_id = "CHANGED-123"
    report.save()

    index = ReportSearchIndex.objects.get(report=report)
    assert index.patient_id == "CHANGED-123"


def test_patient_age_is_mirrored():
    """patient_age is a stored generated column. A BEFORE trigger would read NULL
    here; this test is what pins the triggers to AFTER."""
    report = ReportFactory.create(language=LanguageFactory.create(code="en"))

    report.patient_id = "TOUCH"
    report.save()

    index = ReportSearchIndex.objects.get(report=report)
    report.refresh_from_db()
    assert index.patient_age == report.patient_age
    assert index.patient_age is not None


def test_creation_populates_the_mirrored_scalars():
    """Build + explicit save() is a single INSERT with no follow-up UPDATE.

    ReportFactory.create() would not pin this: its post_generation hooks
    (modalities, the metadata RelatedFactoryList) make factory_boy issue an
    implicit extra save() afterwards, which fires the AFTER UPDATE trigger
    from migration 0004 and would populate these scalars regardless of
    whether the signal's own sync_projection() call exists. build() skips
    those hooks, so the only thing that can fill the scalars here is the
    signal's sync_projection() call on creation.
    """
    report = ReportFactory.build(language=LanguageFactory.create(code="de"))
    report.save()

    index = ReportSearchIndex.objects.get(report=report)
    assert index.language_code == "de"
    assert index.patient_id == report.patient_id


def test_bulk_upsert_populates_the_projection():
    language = LanguageFactory.create(code="en")
    report = ReportFactory.create(language=language, modalities=["CT"])
    group = GroupFactory.create()
    report.groups.add(group)
    ReportSearchIndex.objects.filter(report=report).delete()

    bulk_upsert_report_search_indexes([report.pk])

    index = ReportSearchIndex.objects.get(report=report)
    assert index.language_code == "en"
    assert index.group_ids == [group.pk]
    assert index.modality_codes == ["CT"]


def test_backfill_fills_rows_that_predate_the_projection():
    """Simulates a row written before the projection existed."""
    language = LanguageFactory.create(code="en")
    report = ReportFactory.create(language=language, modalities=["MR"])
    group = GroupFactory.create()
    report.groups.add(group)

    ReportSearchIndex.objects.filter(report=report).update(
        group_ids=[], modality_codes=[], language_code=None, patient_id=None
    )

    from radis.pgsearch.utils.projection import sync_projection

    sync_projection([report.pk])

    index = ReportSearchIndex.objects.get(report=report)
    assert index.group_ids == [group.pk]
    assert index.modality_codes == ["MR"]
    assert index.language_code == "en"
    assert index.patient_id == report.patient_id
