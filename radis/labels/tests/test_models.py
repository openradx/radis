import pytest
from django.db import IntegrityError, transaction

from radis.labels.factories import (
    GateAnswerFactory,
    LabelFactory,
    LabelGroupFactory,
    LabelResultFactory,
)
from radis.labels.models import (
    GateAnswer,
    Label,
    LabelingScanCheckpoint,
    LabelResult,
)
from radis.reports.factories import ReportFactory


@pytest.mark.django_db
def test_group_str_is_plain_name():
    group = LabelGroupFactory.create(name="Chest")
    assert str(group) == "Chest"


@pytest.mark.django_db
def test_label_str_is_plain_name():
    label = LabelFactory.create(name="Metastasis")
    assert str(label) == "Metastasis"


@pytest.mark.django_db
def test_group_delete_cascades_to_labels_and_gate_answers():
    group = LabelGroupFactory.create()
    label = LabelFactory.create(group=group)
    GateAnswerFactory.create(label_group=group)

    group.delete()

    assert not Label.objects.filter(pk=label.pk).exists()
    assert GateAnswer.objects.count() == 0


@pytest.mark.django_db
def test_label_delete_cascades_to_results():
    label = LabelFactory.create()
    result = LabelResultFactory.create(label=label)

    label.delete()

    assert not LabelResult.objects.filter(pk=result.pk).exists()


@pytest.mark.django_db
def test_label_name_is_unique():
    LabelFactory.create(name="pneumonia")
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            LabelFactory.create(name="pneumonia")


@pytest.mark.django_db
def test_group_name_is_unique():
    LabelGroupFactory.create(name="Chest")
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            LabelGroupFactory.create(name="Chest")


@pytest.mark.django_db
def test_result_unique_per_report_label_and_upsert():
    report = ReportFactory.create()
    label = LabelFactory.create()
    LabelResult.objects.create(report=report, label=label, value=LabelResult.Value.PRESENT)

    obj, created = LabelResult.objects.update_or_create(
        report=report, label=label, defaults={"value": LabelResult.Value.ABSENT}
    )
    assert created is False
    assert obj.value == LabelResult.Value.ABSENT
    assert LabelResult.objects.filter(report=report, label=label).count() == 1


@pytest.mark.django_db
def test_gate_answer_unique_per_report_group_and_upsert():
    report = ReportFactory.create()
    group = LabelGroupFactory.create()
    GateAnswer.objects.create(report=report, label_group=group, value=GateAnswer.Value.YES)

    obj, created = GateAnswer.objects.update_or_create(
        report=report, label_group=group, defaults={"value": GateAnswer.Value.NO}
    )
    assert created is False
    assert obj.value == GateAnswer.Value.NO
    assert GateAnswer.objects.filter(report=report, label_group=group).count() == 1


@pytest.mark.django_db
def test_scan_checkpoint_is_singleton():
    first = LabelingScanCheckpoint()
    first.save()
    second = LabelingScanCheckpoint()
    second.save()  # save() forces pk=1, so this updates row 1 rather than inserting a second

    assert LabelingScanCheckpoint.objects.count() == 1


@pytest.mark.django_db
def test_raw_queue_row_delete_nulls_labeling_task_fk(settings):
    settings.PROCRASTINATE_READONLY_MODELS = False
    from django.db import connection
    from procrastinate.contrib.django.models import ProcrastinateJob

    from radis.labels.factories import LabelingJobFactory, LabelingTaskFactory

    row = ProcrastinateJob.objects.create(
        queue_name="llm",
        task_name="radis.labels.tasks.process_labeling_task",
        priority=0,
        args={},
        status="todo",
        attempts=0,
        abort_requested=False,
    )
    task = LabelingTaskFactory.create(job=LabelingJobFactory.create(), queued_job=row)

    # Procrastinate deletes rows via raw SQL, bypassing Django's SET_NULL.
    with connection.cursor() as cursor:
        cursor.execute("DELETE FROM procrastinate_jobs WHERE id = %s", [row.pk])

    task.refresh_from_db()
    assert task.queued_job_id is None
