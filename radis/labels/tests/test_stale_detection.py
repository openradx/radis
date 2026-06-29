import pytest
from django.db.models import F

from radis.labels.factories import (
    GateAnswerFactory,
    LabelFactory,
    LabelGroupFactory,
    LabelResultFactory,
)
from radis.labels.models import GateAnswer, LabelResult


@pytest.mark.django_db
def test_label_result_is_fresh_when_generated_after_label_update():
    label = LabelFactory.create()
    LabelResultFactory.create(label=label)  # generated_at = now, label.updated_at = now

    stale = LabelResult.objects.filter(generated_at__lt=F("label__updated_at"))
    assert stale.count() == 0


@pytest.mark.django_db
def test_label_result_becomes_stale_after_label_edited():
    label = LabelFactory.create()
    result = LabelResultFactory.create(label=label)

    # Editing the label bumps label.updated_at past the result's generated_at.
    label.description = "edited"
    label.save()

    stale = LabelResult.objects.filter(generated_at__lt=F("label__updated_at"))
    assert list(stale.values_list("pk", flat=True)) == [result.pk]


@pytest.mark.django_db
def test_gate_answer_becomes_stale_after_group_edited():
    group = LabelGroupFactory.create()
    answer = GateAnswerFactory.create(label_group=group)

    group.gate_question = "edited?"
    group.save()

    stale = GateAnswer.objects.filter(generated_at__lt=F("label_group__updated_at"))
    assert list(stale.values_list("pk", flat=True)) == [answer.pk]


@pytest.mark.django_db
def test_absent_result_is_treated_as_fresh():
    """A label that came back ABSENT still has a fresh result row — not stale, no re-work."""
    label = LabelFactory.create()
    LabelResultFactory.create(label=label, value=LabelResult.Value.ABSENT)

    stale = LabelResult.objects.filter(generated_at__lt=F("label__updated_at"))
    assert stale.count() == 0
