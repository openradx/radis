import pytest


@pytest.fixture(autouse=True)
def _enable_db(db):
    return db


@pytest.fixture(autouse=True)
def _clear_orphan_queued_job_fks(_enable_db):
    """Reset queued_job_id on LabelingTask before Django's constraint check on teardown.

    Tests that mock ``radis.labels.tasks.app`` may write fake procrastinate job ids
    onto LabelingTask rows; those ids don't exist in ``procrastinate_jobs``, which
    causes Django's deferred FK check at teardown to raise IntegrityError. This
    finalizer runs before Django's ``check_constraints`` and clears the offending
    references so teardown succeeds. Skipped when the transaction is already
    broken (e.g. a test asserted on IntegrityError).
    """
    yield
    from django.db import connection

    if connection.needs_rollback or not connection.is_usable():
        return
    from radis.labels.models import LabelingTask

    LabelingTask.objects.filter(queued_job_id__isnull=False).update(queued_job_id=None)
