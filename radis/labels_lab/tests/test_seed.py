import pytest
from django.core.management import call_command

from radis.labels.factories import LabelFactory, LabelGroupFactory
from radis.labels.models import Label, LabelGroup


@pytest.mark.django_db
def test_seed_creates_groups_with_a_gate_question_and_active_labels():
    call_command("seed_lab_labels")

    chest = LabelGroup.objects.get(name="Chest pathology")
    assert chest.gate_question == "Does this report describe imaging of the chest?"
    pneumonia = Label.objects.get(name="pneumonia")
    assert pneumonia.group == chest
    assert pneumonia.active
    assert pneumonia.description
    assert LabelGroup.objects.count() >= 3
    assert all(group.labels.exists() for group in LabelGroup.objects.all())


@pytest.mark.django_db
def test_seeding_twice_adds_nothing_the_second_time():
    call_command("seed_lab_labels")
    groups, labels = LabelGroup.objects.count(), Label.objects.count()

    call_command("seed_lab_labels")

    assert (LabelGroup.objects.count(), Label.objects.count()) == (groups, labels)


@pytest.mark.django_db
def test_seed_leaves_groups_and_labels_that_already_exist_as_they_are():
    own_chest = LabelGroupFactory.create(name="Chest pathology", gate_question="Is it a chest CT?")
    elsewhere = LabelGroupFactory.create(name="My group")
    own_label = LabelFactory.create(
        group=elsewhere, name="pneumothorax", description="My own definition.", active=False
    )

    call_command("seed_lab_labels")

    own_chest.refresh_from_db()
    own_label.refresh_from_db()
    assert own_chest.gate_question == "Is it a chest CT?"
    assert own_label.group == elsewhere
    assert own_label.description == "My own definition."
    assert not own_label.active
    assert Label.objects.get(name="pneumonia").group == own_chest
