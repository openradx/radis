import pytest
from crispy_forms.layout import Field, Layout

from radis.labels.factories import LabelFactory
from radis.reports.factories import LanguageFactory
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
    choices = form.fields["labels"].choices  # type: ignore

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
    assert form.filters_helper.layout is not None
    field_names = get_layout_field_names(form.filters_helper.layout)

    assert "labels" in field_names


@pytest.mark.django_db
def test_language_field_offers_all_and_is_the_default() -> None:
    """The language filter offers 'All' as a choice and is pre-selected on an
    unbound form, so a plain search genuinely searches every language."""
    LanguageFactory.create(code="en")
    LanguageFactory.create(code="de")

    form = SearchForm()
    field = form.fields["language"]

    assert field.choices[0] == ("", "All")  # type: ignore[index]
    # No data and no initial -> the bound field's rendered value is falsy,
    # which is what makes the browser default to the first ("All") option.
    assert not form["language"].value()


@pytest.mark.django_db
def test_labels_field_absent_from_layout_when_no_active_labels() -> None:
    """With no active labels, the labels field is omitted so no empty listbox renders."""
    LabelFactory.create(name="legacy", active=False)

    form = SearchForm()
    assert form.filters_helper.layout is not None
    field_names = get_layout_field_names(form.filters_helper.layout)

    assert "labels" not in field_names
    assert form.fields["labels"].choices == []  # type: ignore
