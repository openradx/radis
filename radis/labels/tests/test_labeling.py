from unittest.mock import patch

import pytest
from django.test import override_settings

from radis.labels.factories import GateAnswerFactory, LabelFactory, LabelGroupFactory
from radis.labels.models import GateAnswer, LabelResult
from radis.labels.tests.helpers import FakeChatClient
from radis.reports.factories import ReportFactory


def _patch_client(client):
    return patch("radis.labels.labeling.ChatClient", return_value=client)


@pytest.mark.django_db
def test_skips_when_report_body_empty():
    from radis.labels.labeling import label_report

    report = ReportFactory.create(body="   ")
    LabelFactory.create()
    client = FakeChatClient()
    with _patch_client(client):
        label_report(report.pk)

    assert client.gate_calls == []
    assert client.label_calls == []
    assert LabelResult.objects.count() == 0


@pytest.mark.django_db
def test_skips_when_no_active_labels():
    from radis.labels.labeling import label_report

    report = ReportFactory.create(body="lungs clear")
    LabelFactory.create(active=False)
    client = FakeChatClient()
    with _patch_client(client):
        label_report(report.pk)

    assert client.gate_calls == []
    assert GateAnswer.objects.count() == 0


@pytest.mark.django_db
def test_gate_no_skips_group_entirely():
    from radis.labels.labeling import label_report

    report = ReportFactory.create(body="abdomen study")
    group = LabelGroupFactory.create()
    label = LabelFactory.create(group=group)
    client = FakeChatClient(gate_values={group.id: "NO"})
    with _patch_client(client):
        label_report(report.pk)

    assert len(client.gate_calls) == 1
    assert client.label_calls == []  # no label call for a NO-gated group
    assert GateAnswer.objects.get(report=report, label_group=group).value == "NO"
    assert not LabelResult.objects.filter(report=report, label=label).exists()


@pytest.mark.django_db
def test_gate_yes_runs_labels_and_stores_all_buckets():
    from radis.labels.labeling import label_report

    report = ReportFactory.create(body="lungs clear, no effusion")
    group = LabelGroupFactory.create()
    l_present = LabelFactory.create(group=group)
    l_absent = LabelFactory.create(group=group)
    l_unmentioned = LabelFactory.create(group=group)
    client = FakeChatClient(
        gate_values={group.id: "YES"},
        label_values={
            l_present.id: "PRESENT",
            l_absent.id: "ABSENT",
            l_unmentioned.id: "UNMENTIONED",
        },
    )
    with _patch_client(client):
        label_report(report.pk)

    assert LabelResult.objects.get(report=report, label=l_present).value == "PRESENT"
    assert LabelResult.objects.get(report=report, label=l_absent).value == "ABSENT"
    assert LabelResult.objects.get(report=report, label=l_unmentioned).value == "UNMENTIONED"


@pytest.mark.django_db
@override_settings(LABELING_GATE_BATCH_SIZE=10)
def test_gate_batching_two_calls_for_twenty_groups():
    from radis.labels.labeling import label_report

    report = ReportFactory.create(body="study text")
    groups = [LabelGroupFactory.create() for _ in range(20)]
    for g in groups:
        LabelFactory.create(group=g)
    client = FakeChatClient(gate_values={g.id: "NO" for g in groups})
    with _patch_client(client):
        label_report(report.pk)

    assert len(client.gate_calls) == 2
    assert [len(c) for c in client.gate_calls] == [10, 10]


@pytest.mark.django_db
def test_fresh_gate_and_fresh_results_make_zero_llm_calls():
    from radis.labels.labeling import label_report

    report = ReportFactory.create(body="lungs clear")
    group = LabelGroupFactory.create()
    label = LabelFactory.create(group=group)
    GateAnswerFactory.create(report=report, label_group=group, value="YES")
    LabelResult.objects.create(report=report, label=label, value=LabelResult.Value.PRESENT)

    client = FakeChatClient()
    with _patch_client(client):
        label_report(report.pk)

    assert client.gate_calls == []
    assert client.label_calls == []


@pytest.mark.django_db
def test_fresh_gate_yes_with_one_stale_label_runs_only_that_label():
    from radis.labels.labeling import label_report

    report = ReportFactory.create(body="lungs clear")
    group = LabelGroupFactory.create()
    fresh_label = LabelFactory.create(group=group)
    stale_label = LabelFactory.create(group=group)
    GateAnswerFactory.create(report=report, label_group=group, value="YES")
    LabelResult.objects.create(report=report, label=fresh_label, value=LabelResult.Value.PRESENT)
    LabelResult.objects.create(report=report, label=stale_label, value=LabelResult.Value.PRESENT)
    stale_label.description = "edited"
    stale_label.save()

    client = FakeChatClient(label_values={stale_label.id: "ABSENT"})
    with _patch_client(client):
        label_report(report.pk)

    assert client.gate_calls == []
    assert client.label_calls == [[stale_label.id]]
    assert LabelResult.objects.get(report=report, label=stale_label).value == "ABSENT"


@pytest.mark.django_db
def test_gate_flip_yes_to_no_deletes_results_atomically():
    from radis.labels.labeling import label_report

    report = ReportFactory.create(body="lungs clear")
    group = LabelGroupFactory.create()
    label = LabelFactory.create(group=group)
    GateAnswerFactory.create(report=report, label_group=group, value="YES")
    LabelResult.objects.create(report=report, label=label, value=LabelResult.Value.PRESENT)
    group.gate_question = "changed?"
    group.save()

    client = FakeChatClient(gate_values={group.id: "NO"})
    with _patch_client(client):
        label_report(report.pk)

    assert GateAnswer.objects.get(report=report, label_group=group).value == "NO"
    assert not LabelResult.objects.filter(report=report, label__group=group).exists()


@pytest.mark.django_db
def test_stale_gate_new_yes_old_no_runs_all_labels():
    from radis.labels.labeling import label_report

    report = ReportFactory.create(body="lungs clear")
    group = LabelGroupFactory.create()
    label = LabelFactory.create(group=group)
    GateAnswerFactory.create(report=report, label_group=group, value="NO")
    group.gate_question = "changed?"
    group.save()

    client = FakeChatClient(gate_values={group.id: "YES"}, label_values={label.id: "PRESENT"})
    with _patch_client(client):
        label_report(report.pk)

    assert len(client.gate_calls) == 1
    assert client.label_calls == [[label.id]]
    assert LabelResult.objects.get(report=report, label=label).value == "PRESENT"
