import re
from datetime import date
from html import unescape

import pytest
from crispy_forms.layout import Field, Layout
from crispy_forms.utils import render_crispy_form
from django.template.loader import render_to_string
from django.urls import reverse

from radis.labels.factories import LabelFactory
from radis.reports.factories import LanguageFactory, ModalityFactory
from radis.search.forms import DESCRIPTION_MAX_LENGTH, DescriptionRequestForm, SearchForm
from radis.search.layouts import QueryInput


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


def _query_layout_names(form: SearchForm) -> list[str]:
    assert form.query_helper.layout is not None
    names: list[str] = []
    for item in form.query_helper.layout.fields:
        if isinstance(item, QueryInput):
            names.append(item.query_field)
            names.extend(f for f in (item.mode_field, item.description_field) if f)
    return names


@pytest.mark.django_db
def test_semantic_fields_exist_only_with_the_capability() -> None:
    with_capability = SearchForm(semantic=True)
    without = SearchForm()
    for name in ("mode", "description", "min_similarity"):
        assert name in with_capability.fields
        assert name not in without.fields
    assert _query_layout_names(with_capability) == ["query", "mode", "description"]
    assert _query_layout_names(without) == ["query"]


@pytest.mark.django_db
def test_mode_choices_default_to_full_text() -> None:
    form = SearchForm(semantic=True)
    assert form.fields["mode"].choices == [("", "Full-text"), ("semantic", "Semantic")]  # type: ignore[attr-defined]
    bound = SearchForm({"query": "pneumothorax"}, semantic=True)
    assert bound.is_valid()
    assert bound.semantic_mode() is False
    assert SearchForm({"description": "free air", "mode": "semantic"}, semantic=True).is_valid()


@pytest.mark.django_db
def test_description_whitespace_is_collapsed() -> None:
    form = SearchForm({"query": "x", "description": "  pleural \n air  "}, semantic=True)
    assert form.is_valid()
    assert form.cleaned_data["description"] == "pleural air"


@pytest.mark.django_db
def test_description_over_the_limit_is_invalid() -> None:
    form = SearchForm({"query": "x", "description": "a" * 1001}, semantic=True)
    assert not form.is_valid()
    assert "description" in form.errors


@pytest.mark.django_db
def test_query_over_200_characters_is_invalid() -> None:
    form = SearchForm({"query": "a" * 201})
    assert not form.is_valid()
    assert "query" in form.errors


@pytest.mark.django_db
@pytest.mark.parametrize("value", ["1.5", "-0.1"])
def test_min_similarity_outside_unit_interval_is_invalid(value) -> None:
    form = SearchForm(
        {"mode": "semantic", "description": "x", "min_similarity": value}, semantic=True
    )
    assert not form.is_valid()
    assert "min_similarity" in form.errors


@pytest.mark.django_db
def test_min_similarity_is_optional_and_shows_the_default_as_placeholder(settings) -> None:
    settings.SEARCH_SEMANTIC_MIN_SIMILARITY = 0.3
    form = SearchForm({"mode": "semantic", "description": "x"}, semantic=True)
    assert form.is_valid()
    assert form.cleaned_data["min_similarity"] is None
    assert form.fields["min_similarity"].widget.attrs["placeholder"] == "default 0.300"
    assert form.fields["min_similarity"].initial is None


@pytest.mark.django_db
def test_to_filters_carries_every_filter_field() -> None:
    LanguageFactory.create(code="en")
    form = SearchForm(
        {
            "query": "x",
            "language": "en",
            "study_description": "CT",
            "patient_sex": "F",
            "age_from": 20,
            "age_till": 60,
        },
        semantic=True,
    )
    assert form.is_valid(), form.errors
    filters = form.to_filters(7)
    assert filters.group == 7
    assert filters.language == "en"
    assert filters.study_description == "CT"
    assert filters.patient_sex == "F"
    assert (filters.patient_age_from, filters.patient_age_till) == (20, 60)
    assert filters.labels == []


@pytest.mark.django_db
def test_to_filters_carries_labels_modalities_and_dates() -> None:
    LabelFactory.create(name="edema", active=True)
    ModalityFactory.create(code="CT")
    form = SearchForm(
        {
            "query": "x",
            "labels": ["edema"],
            "modalities": ["CT"],
            "study_date_from": "2024-01-02",
            "study_date_till": "2024-03-04",
        }
    )
    assert form.is_valid(), form.errors
    filters = form.to_filters(7)
    assert filters.labels == ["edema"]
    assert filters.modalities == ["CT"]
    assert filters.study_date_from == date(2024, 1, 2)
    assert filters.study_date_till == date(2024, 3, 4)


@pytest.mark.django_db
def test_semantic_mode_follows_the_mode_field_and_the_capability() -> None:
    semantic = SearchForm({"mode": "semantic", "description": "x"}, semantic=True)
    assert semantic.is_valid()
    assert semantic.semantic_mode() is True

    # Without the capability the field does not exist, so a stale ?mode=semantic is ignored.
    stale = SearchForm({"mode": "semantic", "query": "x"})
    assert stale.is_valid()
    assert stale.semantic_mode() is False


def test_description_request_form_limits_match_the_search_form() -> None:
    assert DescriptionRequestForm({"query": "a" * 200, "description": "b" * 1000}).is_valid()
    assert not DescriptionRequestForm({"query": "a" * 201}).is_valid()
    assert not DescriptionRequestForm({"description": "b" * 1001}).is_valid()


def _row_html(form: SearchForm) -> str:
    """The rendered query row with whitespace collapsed, so assertions ignore indentation."""
    return " ".join(render_crispy_form(form, form.query_helper).split())


@pytest.mark.django_db
def test_query_row_without_the_capability_is_the_plain_search_row() -> None:
    html = _row_html(SearchForm())
    assert '<span class="input-group-text">Search</span>' in html
    for absent in ('name="mode"', 'name="description"', "Write from keywords"):
        assert absent not in html


@pytest.mark.django_db
def test_query_row_with_the_capability_renders_the_select_and_the_description() -> None:
    form = SearchForm(
        {"mode": "semantic", "query": "pneumothorax", "description": "free <air>"}, semantic=True
    )
    html = _row_html(form)
    assert '<option value="semantic" selected>Semantic</option>' in html
    assert 'x-model.fill="mode"' in html
    # The keyword input is locked in semantic mode.
    assert ":readonly=\"mode === 'semantic'\"" in unescape(html)
    assert ">free &lt;air&gt;</textarea>" in html
    assert f'hx-post="{reverse("search_description")}"' in html
    assert "Write from keywords" in html


@pytest.mark.django_db
def test_cutoff_field_is_in_the_filters_only_with_the_capability() -> None:
    with_capability = SearchForm(semantic=True)
    html = " ".join(render_crispy_form(with_capability, with_capability.filters_helper).split())
    assert "Minimum similarity" in html
    assert 'name="min_similarity"' in html
    # Disabled outside semantic mode, so the cutoff never rides along in a full-text URL.
    assert ":disabled=\"mode !== 'semantic'\"" in unescape(html)

    without = SearchForm()
    assert "min_similarity" not in render_crispy_form(without, without.filters_helper)


def test_description_partial_escapes_its_value_and_notice() -> None:
    html = render_to_string(
        "search/form_elements/description_input.html",
        {"value": "a </textarea><b>", "notice": "Oops <i>"},
    )
    assert ">a &lt;/textarea&gt;&lt;b&gt;</textarea>" in html
    assert 'hx-swap-oob="innerHTML:#description_notice">Oops &lt;i&gt;</small>' in html
    assert f'maxlength="{DESCRIPTION_MAX_LENGTH}"' in html


def test_description_partial_swaps_only_the_textarea_into_the_page() -> None:
    """The endpoint answers with the whole partial; selecting the textarea out of it keeps
    the page's row and button from being nested inside themselves."""
    html = render_to_string(
        "search/form_elements/description_input.html", {"value": "", "notice": ""}
    )
    assert html.count('id="id_description"') == 1
    assert 'hx-target="#id_description"' in html
    assert 'hx-select="#id_description"' in html


@pytest.mark.django_db
@pytest.mark.parametrize("field", ["age_from", "age_till"])
def test_an_age_off_the_step_is_a_field_error_not_a_crash(field) -> None:
    form = SearchForm({field: "15"})
    assert form.is_valid() is False
    assert field in form.errors


@pytest.mark.django_db
def test_mode_dependent_controls_are_cloaked_until_alpine_starts() -> None:
    """The write button and the cutoff are shown by mode; without x-cloak they would flash
    before Alpine has read the mode."""
    form = SearchForm(semantic=True)
    for helper in (form.query_helper, form.filters_helper):
        tags = re.findall(r"<[^>]*\bx-show=[^>]*>", render_crispy_form(form, helper))
        assert tags
        assert all(re.search(r"\bx-cloak\b", tag) for tag in tags), tags
