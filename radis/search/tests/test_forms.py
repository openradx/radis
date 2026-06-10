import pytest
from crispy_forms.layout import Field, Layout

from radis.labels.factories import LabelFactory
from radis.search.forms import SearchForm


def get_layout_field_names(layout: Layout) -> list[str]:
    """Extract field names from top-level Field objects in a crispy Layout."""
    names: list[str] = []
    for item in layout.fields:
        if isinstance(item, Field):
            names.extend(str(f) for f in item.fields)
    return names


@pytest.mark.django_db
def test_labels_choices_are_active_and_alphabetical() -> None:
    """The labels field lists only active labels, ordered alphabetically by name."""
    LabelFactory.create(name="pneumonia", active=True)
    LabelFactory.create(name="aortic_aneurysm", active=True)
    LabelFactory.create(name="fracture", active=True)
    LabelFactory.create(name="legacy", active=False)

    form = SearchForm()
    choices = form.fields["labels"].choices

    assert choices == [
        ("aortic_aneurysm", "aortic_aneurysm"),
        ("fracture", "fracture"),
        ("pneumonia", "pneumonia"),
    ]


@pytest.mark.django_db
def test_labels_field_is_optional() -> None:
    """A search with no label selection is valid."""
    # create a label so the field has choices and is rendered (submittable as empty)
    LabelFactory.create(name="edema", active=True)

    form = SearchForm(data={"query": "chest"})

    assert form.is_valid(), form.errors
    assert form.cleaned_data["labels"] == []


@pytest.mark.django_db
def test_labels_field_in_layout_when_active_labels_exist() -> None:
    """The labels field is rendered in the filters layout when active labels exist."""
    LabelFactory.create(name="edema", active=True)

    form = SearchForm()
    field_names = get_layout_field_names(form.filters_helper.layout)

    assert "labels" in field_names


@pytest.mark.django_db
def test_labels_field_absent_from_layout_when_no_active_labels() -> None:
    """With no active labels, the labels field is omitted so no empty listbox renders."""
    LabelFactory.create(name="legacy", active=False)

    form = SearchForm()
    field_names = get_layout_field_names(form.filters_helper.layout)

    assert "labels" not in field_names
    assert form.fields["labels"].choices == []
