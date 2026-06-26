import pytest
from adit_radis_shared.accounts.factories import UserFactory
from django.test import override_settings
from django.urls import reverse

from radis.labels.models import LabelingJob

# When running with development settings (FORCE_DEBUG_TOOLBAR=true), the debug-toolbar
# middleware calls render_toolbar() on every response.  That template resolves the
# "djdt:render_panel" URL, which is only registered when the __debug__ URL block is
# mounted (i.e. in the real dev server, not in the test client).  We disable the
# toolbar for all tests in this module to prevent that NoReverseMatch.
_no_toolbar = override_settings(DEBUG_TOOLBAR_CONFIG={"SHOW_TOOLBAR_CALLBACK": lambda r: False})


@_no_toolbar
@pytest.mark.django_db
def test_run_backfill_creates_manual_job(client, monkeypatch):
    monkeypatch.setattr(LabelingJob, "delay", lambda self: None)
    admin_user = UserFactory.create(is_staff=True, is_superuser=True, is_active=True)
    client.force_login(admin_user)

    url = reverse("admin:labels_labelingjob_run_backfill")
    response = client.post(url)

    assert response.status_code == 302
    assert LabelingJob.objects.filter(trigger=LabelingJob.Trigger.MANUAL).count() == 1


@_no_toolbar
@pytest.mark.django_db
def test_run_backfill_conflict_when_active_job_exists(client, monkeypatch):
    from radis.labels.factories import LabelingJobFactory

    monkeypatch.setattr(LabelingJob, "delay", lambda self: None)
    LabelingJobFactory.create(status=LabelingJob.Status.IN_PROGRESS)
    admin_user = UserFactory.create(is_staff=True, is_superuser=True, is_active=True)
    client.force_login(admin_user)

    url = reverse("admin:labels_labelingjob_run_backfill")
    response = client.post(url)

    assert response.status_code == 302
    # Still only the one pre-existing active job — no new one was created.
    assert LabelingJob.objects.filter(status__in=LabelingJob.ACTIVE_STATUSES).count() == 1


@_no_toolbar
@pytest.mark.django_db
def test_label_group_search_works(client):
    """LabelGroupAdmin has search_fields, so autocomplete lookups work correctly."""
    from radis.labels.factories import LabelGroupFactory

    LabelGroupFactory.create(name="Oncology")
    admin_user = UserFactory.create(is_staff=True, is_superuser=True, is_active=True)
    client.force_login(admin_user)

    url = reverse("admin:labels_labelgroup_changelist")
    response = client.get(url, {"q": "Oncology"})
    assert response.status_code == 200
    assert b"Oncology" in response.content


@pytest.mark.django_db
def test_read_only_admins_block_deletion():
    """LabelResult/GateAnswer/LabelingTask admins are fully read-only (no delete)."""
    from django.contrib.admin.sites import site
    from django.test import RequestFactory

    from radis.labels.models import GateAnswer, LabelingTask, LabelResult

    request = RequestFactory().get("/")
    for model in (LabelResult, GateAnswer, LabelingTask):
        model_admin = site._registry[model]
        assert model_admin.has_add_permission(request) is False
        assert model_admin.has_change_permission(request) is False
        assert model_admin.has_delete_permission(request) is False


@pytest.mark.django_db
def test_label_group_duplicate_name_raises_integrity_error():
    """LabelGroup.name has a unique constraint; duplicates must be rejected."""
    from django.db import IntegrityError, transaction

    from radis.labels.factories import LabelGroupFactory

    LabelGroupFactory.create(name="Cardiology")
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            LabelGroupFactory.create(name="Cardiology")


@_no_toolbar
@pytest.mark.django_db
def test_labeling_task_detail_shows_message_and_log(client):
    from radis.core.models import AnalysisTask
    from radis.labels.factories import LabelingJobFactory, LabelingTaskFactory

    job = LabelingJobFactory.create(status=LabelingJob.Status.WARNING)
    task = LabelingTaskFactory.create(
        job=job,
        status=AnalysisTask.Status.WARNING,
        message="1 of 2 reports failed to label.",
        log="Report 42: RuntimeError: LLM exploded",
    )
    admin_user = UserFactory.create(is_staff=True, is_superuser=True, is_active=True)
    client.force_login(admin_user)

    url = reverse("admin:labels_labelingtask_change", args=[task.pk])
    response = client.get(url)

    assert response.status_code == 200
    assert b"1 of 2 reports failed to label." in response.content
    assert b"Report 42: RuntimeError: LLM exploded" in response.content
