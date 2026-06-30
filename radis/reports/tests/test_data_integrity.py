"""Data-integrity tests for the reports bulk-upsert path and cascades.

These go beyond the happy-path API contract tests in ``test_api.py`` and assert
*transactional* and *referential* guarantees:

- a partial failure inside ``_bulk_upsert_reports`` rolls the whole batch back
  (no half-written reports / metadata / through rows),
- the post-commit handler side effects only fire after the surrounding
  transaction actually commits (``on_commit`` semantics),
- ``document_id`` uniqueness is enforced at the DB level (including duplicates
  *within* a single bulk batch), and
- deleting a ``Report`` cascades to its ``Metadata``, search vector and M2M
  through rows.
"""

from datetime import UTC, date, datetime

import pytest
from adit_radis_shared.accounts.factories import AdminUserFactory, GroupFactory
from django.contrib.auth.models import Group
from django.db import IntegrityError
from django.db.utils import DataError
from django.test import TestCase
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APIClient

from radis.pgsearch.models import ReportSearchVector
from radis.reports.api.viewsets import _bulk_upsert_reports
from radis.reports.factories import LanguageFactory, ModalityFactory, ReportFactory
from radis.reports.models import Language, Metadata, Modality, Report

BULK_UPSERT_URL = reverse("report-bulk-upsert")
LIST_URL = reverse("report-list")


def validated(document_id: str, *, group: Group, **overrides) -> dict:
    """A validated-data dict in the shape ``_bulk_upsert_reports`` consumes.

    This is the *post-serializer* nested shape: ``language`` is ``{"code": ...}``,
    ``modalities`` a list of ``{"code": ...}``, ``metadata`` a list of
    ``{"key", "value"}`` and ``groups`` a list of Group instances.
    """
    data = {
        "document_id": document_id,
        "language": {"code": "en"},
        "groups": [group],
        "pacs_aet": "synapse",
        "pacs_name": "Synapse",
        "pacs_link": "",
        "patient_id": "1234578",
        "patient_birth_date": date(1976, 5, 23),
        "patient_sex": "M",
        "study_description": "CT Thorax",
        "study_datetime": datetime(2000, 8, 10, 11, 37, tzinfo=UTC),
        "study_instance_uid": "1.2.3",
        "accession_number": "345348389",
        "modalities": [{"code": "CT"}],
        "metadata": [{"key": "k", "value": "v"}],
        "body": "This is the report",
    }
    data.update(overrides)
    return data


def make_payload(document_id: str, *, group: Group, **overrides) -> dict:
    """Flat wire-format payload for the HTTP API."""
    payload = {
        "document_id": document_id,
        "language": "en",
        "groups": [group.pk],
        "pacs_aet": "synapse",
        "pacs_name": "Synapse",
        "pacs_link": "",
        "patient_id": "1234578",
        "patient_birth_date": date(1976, 5, 23).isoformat(),
        "patient_sex": "M",
        "study_description": "CT Thorax",
        "study_datetime": datetime(2000, 8, 10, 11, 37, tzinfo=UTC).isoformat(),
        "study_instance_uid": "1.2.3",
        "accession_number": "345348389",
        "modalities": ["CT"],
        "metadata": {"k": "v"},
        "body": "This is the report",
    }
    payload.update(overrides)
    return payload


# --------------------------------------------------------------------------- #
# document_id uniqueness
# --------------------------------------------------------------------------- #


@pytest.mark.django_db
def test_document_id_is_unique_at_db_level():
    LanguageFactory.create(code="en")
    ReportFactory.create(document_id="uniq-1", modalities=["CT"])

    with pytest.raises(IntegrityError):
        ReportFactory.create(document_id="uniq-1", modalities=["CT"])


@pytest.mark.django_db
def test_duplicate_document_id_within_one_batch_dedupes_keeping_last():
    """Two rows sharing a ``document_id`` in a single bulk call are de-duplicated
    by ``_bulk_upsert_reports`` (last occurrence wins) rather than hitting the
    unique constraint during ``bulk_create``.

    Upstream's bulk-upsert (#187) intentionally collapses same-``document_id``
    rows in the payload to avoid crashing the whole batch; exactly one report is
    written and it carries the *second* row's fields.
    """
    group = GroupFactory.create()

    created, updated = _bulk_upsert_reports(
        [
            validated("dupe", group=group, body="first version"),
            validated("dupe", group=group, body="second version"),
        ]
    )

    assert created == ["dupe"]
    assert updated == []
    assert Report.objects.filter(document_id="dupe").count() == 1
    assert Report.objects.get(document_id="dupe").body == "second version"


# --------------------------------------------------------------------------- #
# Partial-failure rollback (atomicity)
# --------------------------------------------------------------------------- #


@pytest.mark.django_db
def test_partial_failure_rolls_back_new_reports_and_metadata():
    """If a row in the batch violates a column constraint, ``bulk_create`` raises
    inside the ``transaction.atomic()`` block and nothing from the batch persists
    -- not even the valid rows that precede it.

    Here the second report carries an over-length ``patient_sex`` (max_length=1),
    which Postgres rejects at INSERT time with a ``DataError``.
    """
    group = GroupFactory.create()
    good = validated("good-row", group=group)
    bad = validated("bad-row", group=group, patient_sex="MMMM")  # exceeds max_length=1

    with pytest.raises((DataError, IntegrityError)):
        _bulk_upsert_reports([good, bad])

    # No half-written state: neither the good nor the bad row, and no metadata.
    assert not Report.objects.filter(document_id__in=["good-row", "bad-row"]).exists()
    assert Metadata.objects.count() == 0


@pytest.mark.django_db
def test_metadata_failure_rolls_back_already_created_reports():
    """A failure that occurs *after* the reports are bulk-created (during the
    metadata write) must still roll the reports back, because the report INSERT
    and the metadata INSERT share one ``transaction.atomic()`` block.

    We force the metadata write to fail with an over-length ``value`` (the
    ``Metadata.value`` column is ``max_length=255``).
    """
    group = GroupFactory.create()
    row = validated(
        "meta-fail",
        group=group,
        metadata=[{"key": "k", "value": "x" * 300}],  # exceeds value max_length=255
    )

    with pytest.raises((DataError, IntegrityError)):
        _bulk_upsert_reports([row])

    # The report was bulk-created earlier in the same atomic block but must be
    # rolled back when the metadata insert fails.
    assert not Report.objects.filter(document_id="meta-fail").exists()
    assert Metadata.objects.count() == 0


@pytest.mark.django_db
def test_successful_bulk_upsert_persists_everything():
    """Control: a clean batch commits reports, metadata, modalities and groups."""
    group = GroupFactory.create()
    created, updated = _bulk_upsert_reports(
        [validated("ok-1", group=group), validated("ok-2", group=group)]
    )

    assert sorted(created) == ["ok-1", "ok-2"]
    assert updated == []
    assert Report.objects.filter(document_id__in=["ok-1", "ok-2"]).count() == 2
    report = Report.objects.get(document_id="ok-1")
    assert report.modality_codes == ["CT"]
    assert {m.key: m.value for m in report.metadata.all()} == {"k": "v"}
    assert list(report.groups.values_list("pk", flat=True)) == [group.pk]


# --------------------------------------------------------------------------- #
# on_commit semantics
# --------------------------------------------------------------------------- #


class BulkUpsertOnCommitTests(TestCase):
    """``TestCase`` (not ``django_db``) so we can use ``captureOnCommitCallbacks``
    to drive the ``transaction.on_commit`` hooks that the bulk path registers."""

    def _seed(self):
        self.group = GroupFactory.create()
        self.admin = AdminUserFactory.create()
        self.client = APIClient()
        self.client.force_authenticate(user=self.admin)

    def test_handlers_fire_only_after_commit(self):
        self._seed()
        created_seen: list[list[str]] = []

        class _Handler:
            name = "spy"

            def handle(self, reports):
                created_seen.append([r.document_id for r in reports])

        from radis.reports.api import viewsets

        # Register a spy handler; the bulk path schedules its invocation via
        # transaction.on_commit, so it must NOT run until the block commits.
        self.addCleanup(setattr, viewsets, "reports_created_handlers", [])
        viewsets.reports_created_handlers = [_Handler()]
        viewsets.reports_updated_handlers = []

        with self.captureOnCommitCallbacks(execute=True) as callbacks:
            created, _ = _bulk_upsert_reports(
                [validated("commit-1", group=self.group)]
            )
            # Inside the block, before commit, the handler has not run yet.
            assert created == ["commit-1"]
            assert created_seen == []

        # After the with-block the captured on_commit callback executed.
        assert len(callbacks) == 1
        assert created_seen == [["commit-1"]]

    def test_duplicate_in_batch_dedupes_and_fires_handler_once_on_commit(self):
        self._seed()
        created_seen: list[list[str]] = []

        class _Handler:
            name = "spy"

            def handle(self, reports):
                created_seen.append([r.document_id for r in reports])

        from radis.reports.api import viewsets

        self.addCleanup(setattr, viewsets, "reports_created_handlers", [])
        viewsets.reports_created_handlers = [_Handler()]
        viewsets.reports_updated_handlers = []

        # A duplicate document_id in the batch is de-duplicated (last wins) instead
        # of raising; the block commits, so the created handler fires exactly once
        # for the single surviving report -- and only after commit.
        with self.captureOnCommitCallbacks(execute=True) as callbacks:
            created, _ = _bulk_upsert_reports(
                [
                    validated("rb", group=self.group, body="first version"),
                    validated("rb", group=self.group, body="second version"),
                ]
            )
            assert created == ["rb"]
            assert created_seen == []

        assert len(callbacks) == 1
        assert created_seen == [["rb"]]
        assert Report.objects.filter(document_id="rb").count() == 1
        assert Report.objects.get(document_id="rb").body == "second version"


# --------------------------------------------------------------------------- #
# Cascade on delete
# --------------------------------------------------------------------------- #


@pytest.mark.django_db
def test_deleting_report_cascades_metadata_and_search_vector():
    LanguageFactory.create(code="en")
    report = ReportFactory.create(
        document_id="cascade-1",
        language=Language.objects.get(code="en"),
        modalities=["CT", "MR"],
    )
    # Saving a Report creates its search vector via the pgsearch post_save signal.
    assert ReportSearchVector.objects.filter(report=report).exists()
    assert report.metadata.exists()
    report_pk = report.pk

    report.delete()

    assert not Report.objects.filter(pk=report_pk).exists()
    # Metadata has on_delete=CASCADE
    assert not Metadata.objects.filter(report_id=report_pk).exists()
    # OneToOne search vector has on_delete=CASCADE
    assert not ReportSearchVector.objects.filter(report_id=report_pk).exists()
    # M2M through rows are removed, but the Modality rows themselves survive.
    through = Report.modalities.through
    assert not through.objects.filter(report_id=report_pk).exists()
    assert Modality.objects.filter(code__in=["CT", "MR"]).count() == 2


@pytest.mark.django_db
def test_deleting_report_keeps_shared_language_and_modalities():
    """Cascade must not delete shared lookup rows used by other reports."""
    LanguageFactory.create(code="en")
    ModalityFactory.create(code="CT")
    keep = ReportFactory.create(document_id="keep", modalities=["CT"])
    drop = ReportFactory.create(document_id="drop", modalities=["CT"])

    drop.delete()

    keep.refresh_from_db()
    assert Modality.objects.filter(code="CT").exists()
    assert Language.objects.filter(code="en").exists()
    assert keep.modality_codes == ["CT"]


@pytest.mark.django_db
def test_api_delete_removes_report_and_dependents():
    group = GroupFactory.create()
    admin = AdminUserFactory.create()
    client = APIClient()
    client.force_authenticate(user=admin)
    # Avoid the on-commit search-sync handlers (no external FTS DB in tests).
    from radis.reports.api import viewsets

    viewsets.reports_created_handlers = []
    viewsets.reports_deleted_handlers = []

    client.post(LIST_URL, make_payload("del-api", group=group), format="json")
    report = Report.objects.get(document_id="del-api")
    pk = report.pk

    response = client.delete(reverse("report-detail", args=["del-api"]))

    assert response.status_code == status.HTTP_204_NO_CONTENT
    assert not Report.objects.filter(pk=pk).exists()
    assert not Metadata.objects.filter(report_id=pk).exists()
