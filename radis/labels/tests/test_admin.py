import pytest
from adit_radis_shared.accounts.factories import UserFactory
from django.test import override_settings
from django.urls import reverse

from radis.labels.models import LabelingJob

# Under FORCE_DEBUG_TOOLBAR=true the toolbar middleware resolves "djdt:render_panel", which
# only the dev server's __debug__ URLs mount -> NoReverseMatch in the test client.
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
def test_result_and_gate_admins_block_deletion():
    """LabelResult/GateAnswer admins stay fully read-only (no add/change/delete)."""
    from django.contrib.admin.sites import site
    from django.test import RequestFactory

    from radis.labels.models import GateAnswer, LabelResult

    request = RequestFactory().get("/")
    for model in (LabelResult, GateAnswer):
        model_admin = site._registry[model]
        assert model_admin.has_add_permission(request) is False
        assert model_admin.has_change_permission(request) is False
        assert model_admin.has_delete_permission(request) is False


@pytest.mark.django_db
def test_task_admin_allows_delete_but_not_add_or_change():
    """LabelingTask must allow deletion so a job delete can cascade to its tasks."""
    from django.contrib.admin.sites import site
    from django.test import RequestFactory

    from radis.labels.models import LabelingTask

    request = RequestFactory().get("/")
    model_admin = site._registry[LabelingTask]
    assert model_admin.has_add_permission(request) is False
    assert model_admin.has_change_permission(request) is False
    assert model_admin.has_delete_permission(request) is True


@pytest.mark.django_db
def test_job_admin_blocks_deleting_active_jobs():
    from django.contrib.admin.sites import site
    from django.test import RequestFactory

    from radis.labels.factories import LabelingJobFactory
    from radis.labels.models import LabelingJob

    admin_user = UserFactory.create(is_staff=True, is_superuser=True, is_active=True)
    request = RequestFactory().get("/")
    request.user = admin_user
    model_admin = site._registry[LabelingJob]

    active = LabelingJobFactory.create(status=LabelingJob.Status.IN_PROGRESS)
    done = LabelingJobFactory.create(status=LabelingJob.Status.SUCCESS)

    assert model_admin.has_delete_permission(request, active) is False
    assert model_admin.has_delete_permission(request, done) is True


@pytest.mark.django_db
def test_job_admin_has_no_bulk_delete_action():
    from django.contrib.admin.sites import site
    from django.test import RequestFactory

    from radis.labels.models import LabelingJob

    admin_user = UserFactory.create(is_staff=True, is_superuser=True, is_active=True)
    request = RequestFactory().get("/")
    request.user = admin_user

    model_admin = site._registry[LabelingJob]
    assert "delete_selected" not in model_admin.get_actions(request)


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
def test_cancel_job_view_cancels_cancelable_job(client):
    from radis.core.models import AnalysisTask
    from radis.labels.factories import LabelingJobFactory, LabelingTaskFactory

    job = LabelingJobFactory.create(status=LabelingJob.Status.PENDING)
    task = LabelingTaskFactory.create(job=job, status=AnalysisTask.Status.PENDING)
    admin_user = UserFactory.create(is_staff=True, is_superuser=True, is_active=True)
    client.force_login(admin_user)

    url = reverse("admin:labels_labelingjob_cancel", args=[job.pk])
    response = client.post(url)

    assert response.status_code == 302
    job.refresh_from_db()
    task.refresh_from_db()
    assert job.status == LabelingJob.Status.CANCELED
    assert task.status == AnalysisTask.Status.CANCELED


@_no_toolbar
@pytest.mark.django_db
def test_cancel_job_view_rejects_finished_job(client):
    from radis.labels.factories import LabelingJobFactory

    job = LabelingJobFactory.create(status=LabelingJob.Status.SUCCESS)
    admin_user = UserFactory.create(is_staff=True, is_superuser=True, is_active=True)
    client.force_login(admin_user)

    url = reverse("admin:labels_labelingjob_cancel", args=[job.pk])
    response = client.post(url)

    assert response.status_code == 302
    job.refresh_from_db()
    assert job.status == LabelingJob.Status.SUCCESS


@_no_toolbar
@pytest.mark.django_db
def test_cancel_button_shown_only_when_cancelable(client):
    from radis.labels.factories import LabelingJobFactory

    admin_user = UserFactory.create(is_staff=True, is_superuser=True, is_active=True)
    client.force_login(admin_user)

    active = LabelingJobFactory.create(status=LabelingJob.Status.IN_PROGRESS)
    done = LabelingJobFactory.create(status=LabelingJob.Status.SUCCESS)

    active_url = reverse("admin:labels_labelingjob_change", args=[active.pk])
    done_url = reverse("admin:labels_labelingjob_change", args=[done.pk])

    assert b"Cancel job" in client.get(active_url).content
    assert b"Cancel job" not in client.get(done_url).content


@_no_toolbar
@pytest.mark.django_db
def test_cancel_form_posts_to_cancel_url(client):
    from radis.labels.factories import LabelingJobFactory

    admin_user = UserFactory.create(is_staff=True, is_superuser=True, is_active=True)
    client.force_login(admin_user)

    active = LabelingJobFactory.create(status=LabelingJob.Status.IN_PROGRESS)
    change_url = reverse("admin:labels_labelingjob_change", args=[active.pk])
    cancel_url = reverse("admin:labels_labelingjob_cancel", args=[active.pk])

    content = client.get(change_url).content.decode()
    assert f'action="{cancel_url}"' in content


@pytest.mark.django_db
def test_task_admin_blocks_deleting_tasks_of_active_jobs():
    from django.contrib.admin.sites import site
    from django.test import RequestFactory

    from radis.core.models import AnalysisTask
    from radis.labels.factories import LabelingJobFactory, LabelingTaskFactory
    from radis.labels.models import LabelingJob, LabelingTask

    request = RequestFactory().get("/")
    model_admin = site._registry[LabelingTask]

    active_job = LabelingJobFactory.create(status=LabelingJob.Status.IN_PROGRESS)
    active_task = LabelingTaskFactory.create(job=active_job, status=AnalysisTask.Status.PENDING)
    done_job = LabelingJobFactory.create(status=LabelingJob.Status.SUCCESS)
    done_task = LabelingTaskFactory.create(job=done_job, status=AnalysisTask.Status.SUCCESS)

    assert model_admin.has_delete_permission(request, active_task) is False
    assert model_admin.has_delete_permission(request, done_task) is True
    assert model_admin.has_delete_permission(request) is True  # obj=None: cascade context


@pytest.mark.django_db
def test_task_admin_has_no_bulk_delete_action():
    from django.contrib.admin.sites import site
    from django.test import RequestFactory

    from radis.labels.models import LabelingTask

    admin_user = UserFactory.create(is_staff=True, is_superuser=True, is_active=True)
    request = RequestFactory().get("/")
    request.user = admin_user

    model_admin = site._registry[LabelingTask]
    assert "delete_selected" not in model_admin.get_actions(request)


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


@_no_toolbar
@pytest.mark.django_db
def test_job_change_page_has_no_view_on_site_button(client):
    from radis.labels.factories import LabelingJobFactory

    admin_user = UserFactory.create(is_staff=True, is_superuser=True, is_active=True)
    client.force_login(admin_user)

    job = LabelingJobFactory.create(status=LabelingJob.Status.SUCCESS)
    url = reverse("admin:labels_labelingjob_change", args=[job.pk])

    content = client.get(url).content.decode()
    # get_absolute_url points back at this very admin page (and the
    # sites-framework redirect is broken in dev), so the button is useless.
    assert "View on site" not in content


@pytest.mark.django_db
def test_stale_columns_reflect_report_update():
    from django.contrib import admin as django_admin

    from radis.labels.admin import GateAnswerAdmin, LabelResultAdmin
    from radis.labels.factories import GateAnswerFactory, LabelResultFactory
    from radis.labels.models import GateAnswer, LabelResult

    result = LabelResultFactory.create()
    answer = GateAnswerFactory.create(report=result.report)
    result_admin = LabelResultAdmin(LabelResult, django_admin.site)
    gate_admin = GateAnswerAdmin(GateAnswer, django_admin.site)
    assert not result_admin.is_stale(result)
    assert not gate_admin.is_stale(answer)

    result.report.body = "changed"
    result.report.save()
    result.refresh_from_db()
    answer.refresh_from_db()

    assert result_admin.is_stale(result)
    assert gate_admin.is_stale(answer)


# --- Definition-change confirmation on the Label and Label group change forms ---

CONFIRMATION_TEMPLATE = "admin/labels/definition_change_confirmation.html"


def _login_admin(client):
    admin_user = UserFactory.create(is_staff=True, is_superuser=True, is_active=True)
    client.force_login(admin_user)
    return admin_user


def _label_post_data(label, **overrides):
    data = {
        "name": label.name,
        "description": label.description,
        "group": str(label.group_id),
        "active": "on" if label.active else "",
        "_save": "Save",
    }
    data.update(overrides)
    return {k: v for k, v in data.items() if v != ""}


def _rendered_confirmation(response) -> bool:
    return any(t.name == CONFIRMATION_TEMPLATE for t in response.templates)


def _fingerprint(obj) -> str:
    obj.refresh_from_db()
    return obj.definition_updated_at.isoformat()


@_no_toolbar
@pytest.mark.django_db
def test_label_change_form_get_has_no_confirmation(client):
    from radis.labels.factories import LabelResultFactory

    result = LabelResultFactory.create()
    _login_admin(client)

    response = client.get(reverse("admin:labels_label_change", args=[result.label.pk]))

    assert response.status_code == 200
    assert not _rendered_confirmation(response)


@_no_toolbar
@pytest.mark.django_db
def test_label_unchanged_save_goes_straight_through(client):
    from radis.labels.factories import LabelResultFactory

    result = LabelResultFactory.create()
    label = result.label
    before = label.definition_updated_at
    _login_admin(client)

    response = client.post(
        reverse("admin:labels_label_change", args=[label.pk]), _label_post_data(label)
    )

    assert response.status_code == 302
    label.refresh_from_db()
    result.refresh_from_db()
    assert label.definition_updated_at == before
    assert not result.is_stale


@_no_toolbar
@pytest.mark.django_db
def test_label_crlf_resubmission_goes_straight_through(client):
    """A browser submits the textarea with CRLF; an LF row saved untouched must stay fresh."""
    from radis.labels.factories import LabelFactory, LabelResultFactory

    label = LabelFactory.create(description="Line one\nLine two")
    result = LabelResultFactory.create(label=label)
    before = label.definition_updated_at
    _login_admin(client)

    response = client.post(
        reverse("admin:labels_label_change", args=[label.pk]),
        _label_post_data(label, description="Line one\r\nLine two  "),
    )

    assert response.status_code == 302
    label.refresh_from_db()
    result.refresh_from_db()
    assert label.description == "Line one\nLine two"
    assert label.definition_updated_at == before
    assert not result.is_stale


@_no_toolbar
@pytest.mark.django_db
def test_label_active_toggle_saves_without_confirmation(client):
    from radis.labels.factories import LabelResultFactory

    result = LabelResultFactory.create()
    label = result.label
    _login_admin(client)

    response = client.post(
        reverse("admin:labels_label_change", args=[label.pk]), _label_post_data(label, active="")
    )

    assert response.status_code == 302
    label.refresh_from_db()
    result.refresh_from_db()
    assert label.active is False
    assert not result.is_stale


@_no_toolbar
@pytest.mark.django_db
def test_label_description_change_renders_confirmation_without_saving(client):
    from radis.labels.factories import LabelResultFactory

    result = LabelResultFactory.create()
    label = result.label
    LabelResultFactory.create(label=label)  # a second report carrying this label
    old_description = label.description
    before = label.definition_updated_at
    _login_admin(client)

    response = client.post(
        reverse("admin:labels_label_change", args=[label.pk]),
        _label_post_data(label, description="New definition"),
    )

    assert response.status_code == 200
    assert _rendered_confirmation(response)
    content = response.content.decode()
    assert "<strong>2</strong> existing label results" in content
    assert 'name="description" value="New definition"' in content
    assert f'name="_confirm_definition_change" value="{_fingerprint(label)}"' in content
    assert 'method="post" action=""' in content
    assert "Run backfill now" in content
    label.refresh_from_db()
    assert label.description == old_description
    assert label.definition_updated_at == before


@_no_toolbar
@pytest.mark.django_db
def test_label_confirmed_description_change_saves_and_marks_stale(client):
    from radis.labels.factories import LabelResultFactory

    result = LabelResultFactory.create()
    label = result.label
    before = label.definition_updated_at
    _login_admin(client)

    response = client.post(
        reverse("admin:labels_label_change", args=[label.pk]),
        _label_post_data(
            label, description="New definition", _confirm_definition_change=_fingerprint(label)
        ),
    )

    assert response.status_code == 302
    assert response.url == reverse("admin:labels_label_changelist")
    label.refresh_from_db()
    result.refresh_from_db()
    assert label.description == "New definition"
    assert label.definition_updated_at > before
    assert result.is_stale


@_no_toolbar
@pytest.mark.django_db
def test_confirmation_preserves_changelist_filters_and_continue(client):
    from radis.labels.factories import LabelResultFactory

    result = LabelResultFactory.create()
    label = result.label
    change_url = reverse("admin:labels_label_change", args=[label.pk])
    filtered_url = change_url + "?_changelist_filters=active__exact%3D1"
    _login_admin(client)

    data = _label_post_data(label, description="New definition")
    data.pop("_save")
    data["_continue"] = "Save and continue editing"
    response = client.post(filtered_url, data)
    assert response.status_code == 200
    assert _rendered_confirmation(response)
    assert 'name="_continue"' in response.content.decode()

    # The page posts back to the very same URL, query string included.
    data["_confirm_definition_change"] = _fingerprint(label)
    response = client.post(filtered_url, data)

    assert response.status_code == 302
    assert response.url.startswith(change_url)
    assert "_changelist_filters=active__exact%3D1" in response.url


@_no_toolbar
@pytest.mark.django_db
def test_invalid_form_with_definition_change_shows_errors_not_confirmation(client):
    from radis.labels.factories import LabelResultFactory

    result = LabelResultFactory.create()
    label = result.label
    _login_admin(client)

    response = client.post(
        reverse("admin:labels_label_change", args=[label.pk]),
        _label_post_data(label, name="", description="New definition"),
    )

    assert response.status_code == 200
    assert not _rendered_confirmation(response)
    assert "adminform" in response.context
    assert response.context["adminform"].form.errors


@_no_toolbar
@pytest.mark.django_db
def test_definition_change_without_results_needs_no_confirmation(client):
    from radis.labels.factories import LabelFactory

    label = LabelFactory.create()
    _login_admin(client)

    response = client.post(
        reverse("admin:labels_label_change", args=[label.pk]),
        _label_post_data(label, description="New definition"),
    )

    assert response.status_code == 302
    label.refresh_from_db()
    assert label.description == "New definition"


@_no_toolbar
@pytest.mark.django_db
def test_label_add_form_never_confirms(client):
    from radis.labels.factories import LabelGroupFactory
    from radis.labels.models import Label

    group = LabelGroupFactory.create()
    _login_admin(client)

    response = client.post(
        reverse("admin:labels_label_add"),
        {"name": "new", "description": "A definition", "group": str(group.pk), "_save": "Save"},
    )

    assert response.status_code == 302
    assert Label.objects.filter(name="new").exists()


@_no_toolbar
@pytest.mark.django_db
def test_group_gate_question_change_confirms_with_gate_counts(client):
    from radis.labels.factories import GateAnswerFactory, LabelGroupFactory
    from radis.labels.models import GateAnswer

    group = LabelGroupFactory.create()
    yes = GateAnswerFactory.create(label_group=group, value=GateAnswer.Value.YES)
    no = GateAnswerFactory.create(label_group=group, value=GateAnswer.Value.NO)
    before = group.definition_updated_at
    _login_admin(client)
    url = reverse("admin:labels_labelgroup_change", args=[group.pk])
    data = {"name": group.name, "gate_question": "Is this a chest study?", "_save": "Save"}

    response = client.post(url, data)
    assert response.status_code == 200
    assert _rendered_confirmation(response)
    content = response.content.decode()
    assert "<strong>2</strong> existing gate answers" in content
    assert "1 of them currently answer Yes" in content
    assert "flips to Yes" in content
    group.refresh_from_db()
    assert group.definition_updated_at == before

    response = client.post(url, {**data, "_confirm_definition_change": _fingerprint(group)})
    assert response.status_code == 302
    group.refresh_from_db()
    yes.refresh_from_db()
    no.refresh_from_db()
    assert group.gate_question == "Is this a chest study?"
    assert group.definition_updated_at > before
    assert yes.is_stale and no.is_stale


@_no_toolbar
@pytest.mark.django_db
def test_group_unchanged_save_goes_straight_through(client):
    from radis.labels.factories import GateAnswerFactory

    answer = GateAnswerFactory.create()
    group = answer.label_group
    before = group.definition_updated_at
    _login_admin(client)

    response = client.post(
        reverse("admin:labels_labelgroup_change", args=[group.pk]),
        {"name": group.name, "gate_question": group.gate_question, "_save": "Save"},
    )

    assert response.status_code == 302
    group.refresh_from_db()
    answer.refresh_from_db()
    assert group.definition_updated_at == before
    assert not answer.is_stale


@_no_toolbar
@pytest.mark.django_db
def test_label_group_move_confirms_with_group_names_and_marks_stale(client):
    from radis.labels.factories import LabelGroupFactory, LabelResultFactory

    result = LabelResultFactory.create()
    label = result.label
    old_group = label.group
    new_group = LabelGroupFactory.create(name="Abdomen")
    before = label.definition_updated_at
    _login_admin(client)
    url = reverse("admin:labels_label_change", args=[label.pk])

    response = client.post(url, _label_post_data(label, group=str(new_group.pk)))

    assert response.status_code == 200
    assert _rendered_confirmation(response)
    content = response.content.decode()
    assert f"<del>{old_group.name}</del>" in content
    assert "<ins>Abdomen</ins>" in content
    label.refresh_from_db()
    assert label.group_id == old_group.pk

    response = client.post(
        url,
        _label_post_data(
            label, group=str(new_group.pk), _confirm_definition_change=_fingerprint(label)
        ),
    )

    assert response.status_code == 302
    label.refresh_from_db()
    result.refresh_from_db()
    assert label.group_id == new_group.pk
    assert label.definition_updated_at > before
    assert result.is_stale


@_no_toolbar
@pytest.mark.django_db
def test_view_only_user_gets_403_not_confirmation(client):
    from django.contrib.auth.models import Permission

    from radis.labels.factories import LabelResultFactory

    result = LabelResultFactory.create()
    label = result.label
    viewer = UserFactory.create(is_staff=True, is_active=True)
    viewer.user_permissions.add(Permission.objects.get(codename="view_label"))
    client.force_login(viewer)

    response = client.post(
        reverse("admin:labels_label_change", args=[label.pk]),
        _label_post_data(label, description="New definition"),
    )

    assert response.status_code == 403
    assert not _rendered_confirmation(response)
    label.refresh_from_db()
    assert label.description != "New definition"


@_no_toolbar
@pytest.mark.django_db
def test_save_as_new_skips_confirmation(client):
    from radis.labels.factories import LabelResultFactory
    from radis.labels.models import Label

    result = LabelResultFactory.create()
    label = result.label
    _login_admin(client)

    response = client.post(
        reverse("admin:labels_label_change", args=[label.pk]),
        _label_post_data(label, name="copy", description="Other text", _saveasnew="Save as new"),
    )

    assert response.status_code == 302
    assert Label.objects.filter(name="copy", description="Other text").exists()
    label.refresh_from_db()
    result.refresh_from_db()
    assert label.name != "copy"
    assert not result.is_stale


@_no_toolbar
@pytest.mark.django_db
def test_confirmation_round_trip_in_popup(client):
    from radis.labels.factories import LabelResultFactory

    result = LabelResultFactory.create()
    label = result.label
    _login_admin(client)
    url = reverse("admin:labels_label_change", args=[label.pk])
    data = _label_post_data(label, description="New definition", _popup="1")

    response = client.post(url, data)
    assert response.status_code == 200
    assert _rendered_confirmation(response)
    assert response.context["is_popup"] is True
    assert 'name="_popup"' in response.content.decode()

    response = client.post(url, {**data, "_confirm_definition_change": _fingerprint(label)})

    assert response.status_code == 200
    assert any(t.name == "admin/popup_response.html" for t in response.templates)
    label.refresh_from_db()
    assert label.description == "New definition"


@_no_toolbar
@pytest.mark.django_db
def test_inactive_label_confirmation_says_backfills_skip_it(client):
    from radis.labels.factories import LabelFactory, LabelResultFactory

    label = LabelFactory.create(active=False)
    LabelResultFactory.create(label=label)
    _login_admin(client)

    response = client.post(
        reverse("admin:labels_label_change", args=[label.pk]),
        _label_post_data(label, description="New definition"),
    )

    assert response.status_code == 200
    assert _rendered_confirmation(response)
    assert "This label is inactive" in response.content.decode()


@_no_toolbar
@pytest.mark.django_db
def test_stale_fingerprint_shows_confirmation_again_instead_of_saving(client):
    from radis.labels.factories import LabelResultFactory

    result = LabelResultFactory.create()
    label = result.label
    _login_admin(client)
    url = reverse("admin:labels_label_change", args=[label.pk])
    data = _label_post_data(label, description="My definition")
    seen_fingerprint = _fingerprint(label)
    client.post(url, data)

    # Someone else changes the definition between the confirmation page and the confirm click.
    label.description = "Their definition"
    label.save()

    response = client.post(url, {**data, "_confirm_definition_change": seen_fingerprint})

    assert response.status_code == 200
    assert _rendered_confirmation(response)
    content = response.content.decode()
    assert "changed by someone else" in content
    assert "<del>Their definition</del>" in content
    label.refresh_from_db()
    assert label.description == "Their definition"

    response = client.post(url, {**data, "_confirm_definition_change": _fingerprint(label)})

    assert response.status_code == 302
    label.refresh_from_db()
    assert label.description == "My definition"
