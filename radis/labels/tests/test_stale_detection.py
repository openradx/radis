import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext

from radis.labels.factories import (
    GateAnswerFactory,
    LabelFactory,
    LabelGroupFactory,
    LabelResultFactory,
)
from radis.labels.models import Label, LabelGroup, LabelResult, normalize_definition_text


@pytest.mark.django_db
def test_label_result_is_fresh_when_generated_after_label_creation():
    label = LabelFactory.create()
    result = LabelResultFactory.create(label=label)

    assert label.definition_updated_at <= label.updated_at
    assert not result.is_stale


@pytest.mark.django_db
def test_label_result_becomes_stale_after_description_edited():
    label = LabelFactory.create()
    result = LabelResultFactory.create(label=label)

    label.description = "edited"
    label.save()
    result.refresh_from_db()

    assert result.is_stale


@pytest.mark.django_db
def test_label_result_becomes_stale_after_label_renamed():
    """The name is the schema field key the LLM answers under, so it is part of the definition."""
    label = LabelFactory.create()
    result = LabelResultFactory.create(label=label)

    label.name = "renamed"
    label.save()
    result.refresh_from_db()

    assert result.is_stale


@pytest.mark.django_db
def test_noop_save_keeps_label_result_fresh():
    label = LabelFactory.create()
    result = LabelResultFactory.create(label=label)
    before = label.definition_updated_at

    label.save()
    label.refresh_from_db()
    result.refresh_from_db()

    assert label.definition_updated_at == before
    assert label.updated_at > before  # the audit timestamp still moves
    assert not result.is_stale


@pytest.mark.django_db
def test_toggling_active_keeps_label_result_fresh():
    label = LabelFactory.create()
    result = LabelResultFactory.create(label=label)

    label.active = False
    label.save()
    label.active = True
    label.save()
    result.refresh_from_db()

    assert not result.is_stale


@pytest.mark.django_db
def test_moving_label_to_another_group_marks_label_result_stale():
    """The group decides which gate answer the results were produced under."""
    label = LabelFactory.create()
    result = LabelResultFactory.create(label=label)

    label.group = LabelGroupFactory.create()
    label.save()
    result.refresh_from_db()

    assert result.is_stale


@pytest.mark.django_db
def test_reassigning_the_same_group_keeps_label_result_fresh():
    label = LabelFactory.create()
    result = LabelResultFactory.create(label=label)
    before = label.definition_updated_at

    label.group = LabelGroup.objects.get(pk=label.group_id)
    label.save()
    label.refresh_from_db()
    result.refresh_from_db()

    assert label.definition_updated_at == before
    assert not result.is_stale


@pytest.mark.django_db
def test_save_with_update_fields_outside_definition_keeps_fresh():
    label = LabelFactory.create()
    result = LabelResultFactory.create(label=label)

    label.description = "edited"  # not persisted: description is not in update_fields
    label.active = False
    label.save(update_fields=["active"])
    label.refresh_from_db()
    result.refresh_from_db()

    assert label.description != "edited"
    assert not result.is_stale


@pytest.mark.django_db
def test_save_with_update_fields_on_unchanged_definition_keeps_fresh():
    label = LabelFactory.create()
    result = LabelResultFactory.create(label=label)
    before = label.definition_updated_at

    label.save(update_fields=["description", "group"])
    label.refresh_from_db()
    result.refresh_from_db()

    assert label.definition_updated_at == before
    assert not result.is_stale


@pytest.mark.django_db
def test_save_accepts_update_fields_as_an_iterator():
    label = LabelFactory.create()
    result = LabelResultFactory.create(label=label)

    label.description = "edited"
    label.save(update_fields=iter(["description"]))
    label.refresh_from_db()
    result.refresh_from_db()

    assert label.description == "edited"
    assert result.is_stale


@pytest.mark.django_db
def test_save_of_deferred_instance_stays_narrow_and_keeps_fresh():
    """A field that was never loaded cannot have been edited, so it is neither read (which
    would un-defer it) nor written back."""
    label = LabelFactory.create()
    result = LabelResultFactory.create(label=label)
    before = label.definition_updated_at

    deferred = Label.objects.only("id", "active").get(pk=label.pk)
    deferred.active = False
    with CaptureQueriesContext(connection) as queries:
        deferred.save()
    updates = [q["sql"] for q in queries if q["sql"].startswith("UPDATE")]
    label.refresh_from_db()
    result.refresh_from_db()

    assert len(updates) == 1
    assert '"description"' not in updates[0]
    assert label.active is False
    assert label.definition_updated_at == before
    assert not result.is_stale


@pytest.mark.django_db
def test_save_with_update_fields_on_definition_persists_bump():
    label = LabelFactory.create()
    result = LabelResultFactory.create(label=label)
    before = label.definition_updated_at

    label.description = "edited"
    label.save(update_fields=["description"])
    label.refresh_from_db()
    result.refresh_from_db()

    assert label.description == "edited"
    assert label.definition_updated_at > before
    assert result.is_stale


@pytest.mark.django_db
def test_gate_answer_becomes_stale_after_gate_question_edited():
    group = LabelGroupFactory.create()
    answer = GateAnswerFactory.create(label_group=group)

    group.gate_question = "edited?"
    group.save()
    answer.refresh_from_db()

    assert answer.is_stale


@pytest.mark.django_db
def test_gate_answer_becomes_stale_after_group_renamed():
    group = LabelGroupFactory.create()
    answer = GateAnswerFactory.create(label_group=group)

    group.name = "renamed"
    group.save()
    answer.refresh_from_db()

    assert answer.is_stale


@pytest.mark.django_db
def test_noop_group_save_keeps_gate_answer_fresh():
    group = LabelGroupFactory.create()
    answer = GateAnswerFactory.create(label_group=group)
    before = group.definition_updated_at

    group.save()
    group.refresh_from_db()
    answer.refresh_from_db()

    assert group.definition_updated_at == before
    assert not answer.is_stale


@pytest.mark.django_db
def test_group_edit_does_not_stale_its_labels_results():
    group = LabelGroupFactory.create()
    label = LabelFactory.create(group=group)
    result = LabelResultFactory.create(label=label)

    group.gate_question = "edited?"
    group.save()
    result.refresh_from_db()

    assert not result.is_stale


@pytest.mark.django_db
def test_absent_result_is_treated_as_fresh():
    """A label that came back ABSENT still has a fresh result row — not stale, no re-work."""
    label = LabelFactory.create()
    result = LabelResultFactory.create(label=label, value=LabelResult.Value.ABSENT)

    assert not result.is_stale


# --- Normalization: line endings and surrounding whitespace are not definition changes ---


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("a\r\nb", "a\nb"),
        ("a\rb", "a\nb"),
        ("  a\nb  \n", "a\nb"),
        ("a\nb", "a\nb"),
    ],
)
def test_normalize_definition_text(raw, expected):
    assert normalize_definition_text(raw) == expected
    assert normalize_definition_text(expected) == expected  # idempotent


@pytest.mark.django_db
def test_crlf_resubmission_of_same_text_keeps_fresh_and_normalizes_storage():
    """Browsers submit textarea content with CRLF; that must not count as an edit."""
    label = LabelFactory.create(description="Line one\nLine two")
    result = LabelResultFactory.create(label=label)
    before = label.definition_updated_at

    label.description = "Line one\r\nLine two  \n"
    label.save()
    label.refresh_from_db()
    result.refresh_from_db()

    assert label.description == "Line one\nLine two"
    assert label.definition_updated_at == before
    assert not result.is_stale


@pytest.mark.django_db
def test_crlf_row_written_past_save_then_saved_unchanged_keeps_fresh():
    """Text written past save() (QuerySet.update(), raw SQL) is un-normalized; an untouched
    save of such a row must stay fresh."""
    label = LabelFactory.create(description="Line one\nLine two")
    Label.objects.filter(pk=label.pk).update(description="Line one\r\nLine two")
    result = LabelResultFactory.create(label=label)
    label.refresh_from_db()
    before = label.definition_updated_at

    label.save()
    label.refresh_from_db()
    result.refresh_from_db()

    assert label.description == "Line one\nLine two"
    assert label.definition_updated_at == before
    assert not result.is_stale


@pytest.mark.django_db
def test_create_normalizes_definition_text():
    group = LabelGroupFactory.create(gate_question="  Chest?\r\n")
    label = LabelFactory.create(group=group, name=" edema ", description="Fluid.\r\n")

    assert LabelGroup.objects.get(pk=group.pk).gate_question == "Chest?"
    label.refresh_from_db()
    assert (label.name, label.description) == ("edema", "Fluid.")
