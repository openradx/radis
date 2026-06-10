# Label Search Filter Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the typed `label:<name>` search syntax with a multi-select listbox of active labels in the search Filters panel, and switch multi-label matching from AND to OR.

**Architecture:** Add a `labels` `MultipleChoiceField` to `SearchForm` (active labels, alphabetical), thread the selection through `SearchView` into `SearchFilters`, and rewrite the pgsearch provider's label clause to match reports surfacing ANY selected label. Remove the now-dead `label:` query parser and its tests.

**Tech Stack:** Django 6, django-crispy-forms 2.5, django-tables2/django-filter (unused here), pytest + pytest-django, factory-boy. Spec: `docs/superpowers/specs/2026-06-10-label-search-filter-design.md`.

> **IMPORTANT — no commits.** The user will handle all git commits manually. Do NOT run `git commit` or `git add` in any step. Where a task ends, stop after the tests pass and report status.

---

## File Structure

- `radis/pgsearch/providers.py` — `_build_filter_query`: label clause AND → OR (Task 1).
- `radis/search/site.py` — `SearchFilters.labels` docstring: ALL → ANY (Task 1).
- `radis/labels/tests/test_search_filter.py` — provider tests updated to OR; old typed-syntax view test removed (Tasks 1 & 3).
- `radis/search/forms.py` — new `labels` field + conditional layout entry (Task 2).
- `radis/search/tests/test_forms.py` — new test file for the form field (Task 2).
- `radis/search/views.py` — read `labels` from the form; drop `extract_label_filters` (Task 3).
- `radis/search/tests/test_views.py` — new test that the view threads selected labels into `SearchFilters` (Task 3).
- `radis/labels/query.py` — deleted (Task 3).
- `radis/labels/tests/unit/test_query.py` — deleted (Task 3).
- `CLAUDE.md` — docs reference to the `label:` filter updated to the widget (Task 4).

**Test command (this project):** `uv run cli test -- <path> -v`. The `--` forwards args to pytest. These tests hit the database and require the dev containers to be running.

---

### Task 1: Switch provider label matching from AND to OR

**Files:**
- Modify: `radis/labels/tests/test_search_filter.py` (replace the multi-label test)
- Modify: `radis/pgsearch/providers.py:86-95`
- Modify: `radis/search/site.py:46-47`

- [ ] **Step 1: Replace the AND test with an OR test**

In `radis/labels/tests/test_search_filter.py`, delete the entire
`test_label_filter_requires_all_labels` function (lines 50-74) and replace it
with this OR test:

```python
@pytest.mark.django_db
def test_label_filter_matches_any_label() -> None:
    """When multiple labels are requested, a report surfacing ANY of them matches (OR)."""
    language = Language.objects.get_or_create(code="en")[0]

    # report_edema surfaces only "edema"
    report_edema = ReportFactory.create(language=language)
    label_edema = LabelFactory.create(name="edema")
    label_pneumonia = LabelFactory.create(name="pneumonia")
    LabelResultFactory.create(
        report=report_edema, label=label_edema, value=LabelResult.Value.PRESENT
    )

    # report_pneumonia surfaces only "pneumonia"
    report_pneumonia = ReportFactory.create(language=language)
    LabelResultFactory.create(
        report=report_pneumonia, label=label_pneumonia, value=LabelResult.Value.PRESENT
    )

    # report_neither surfaces nothing relevant
    report_neither = ReportFactory.create(language=language)

    fq = _build_filter_query(SearchFilters(group=0, labels=["edema", "pneumonia"]))
    matched_ids = set(ReportSearchVector.objects.filter(fq).values_list("report_id", flat=True))

    assert report_edema.pk in matched_ids
    assert report_pneumonia.pk in matched_ids
    assert report_neither.pk not in matched_ids
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run cli test -- radis/labels/tests/test_search_filter.py::test_label_filter_matches_any_label -v`
Expected: FAIL — with the current AND implementation, `report_edema` and
`report_pneumonia` are each missing one of the two labels, so neither matches
and the assertions fail.

- [ ] **Step 3: Rewrite the label clause in the provider**

In `radis/pgsearch/providers.py`, replace the existing block (lines 86-95):

```python
    if filters.labels:
        from radis.labels.models import LabelResult
        from radis.reports.models import Report

        for name in filters.labels:
            surfacing_report_ids = Report.objects.filter(
                label_results__label__name=name,
                label_results__value__in=LabelResult.SURFACING_VALUES,
            ).values("pk")
            fq &= Q(report__in=surfacing_report_ids)
```

with:

```python
    if filters.labels:
        from radis.labels.models import LabelResult
        from radis.reports.models import Report

        surfacing_report_ids = Report.objects.filter(
            label_results__label__name__in=filters.labels,
            label_results__value__in=LabelResult.SURFACING_VALUES,
        ).values("pk")
        fq &= Q(report__in=surfacing_report_ids)
```

- [ ] **Step 4: Update the `SearchFilters.labels` docstring**

In `radis/search/site.py`, lines 46-47, change:

```python
        - labels: only reports having a surfacing-bucket (PRESENT/LIKELY/POSSIBLE) result for
          ALL of the given label names
```

to:

```python
        - labels: only reports having a surfacing-bucket (PRESENT/LIKELY/POSSIBLE) result for
          ANY of the given label names
```

- [ ] **Step 5: Run the provider tests to verify they pass**

Run: `uv run cli test -- radis/labels/tests/test_search_filter.py -v -k "filter"`
Expected: PASS for `test_label_filter_includes_surfacing_result`,
`test_label_filter_excludes_absent_result`, and
`test_label_filter_matches_any_label`.
(`test_search_view_extracts_label_filter_from_query` is still present and may
still pass at this point — it is removed in Task 3.)

---

### Task 2: Add the `labels` field to SearchForm

**Files:**
- Create: `radis/search/tests/test_forms.py`
- Modify: `radis/search/forms.py`

- [ ] **Step 1: Write the failing form tests**

Create `radis/search/tests/test_forms.py`:

```python
import pytest

from radis.labels.factories import LabelFactory
from radis.search.forms import SearchForm


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
    LabelFactory.create(name="edema", active=True)

    form = SearchForm(data={"query": "chest"})

    assert form.is_valid(), form.errors
    assert form.cleaned_data["labels"] == []


@pytest.mark.django_db
def test_labels_field_in_layout_when_active_labels_exist() -> None:
    """The labels field is rendered in the filters layout when active labels exist."""
    LabelFactory.create(name="edema", active=True)

    form = SearchForm()
    field_names = [pointer.name for pointer in form.filters_helper.layout.get_field_names()]

    assert "labels" in field_names


@pytest.mark.django_db
def test_labels_field_absent_from_layout_when_no_active_labels() -> None:
    """With no active labels, the labels field is omitted so no empty listbox renders."""
    LabelFactory.create(name="legacy", active=False)

    form = SearchForm()
    field_names = [pointer.name for pointer in form.filters_helper.layout.get_field_names()]

    assert "labels" not in field_names
    assert form.fields["labels"].choices == []
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run cli test -- radis/search/tests/test_forms.py -v`
Expected: FAIL with `KeyError: 'labels'` (the field does not exist yet).

- [ ] **Step 3: Add the field declaration**

In `radis/search/forms.py`, add the import near the top (with the existing
reports import on line 9):

```python
from radis.labels.models import Label
from radis.reports.models import Language, Modality
```

Add the field to `SearchForm`, directly after the `modalities` field
(after line 23):

```python
    modalities = forms.MultipleChoiceField(required=False, choices=[])
    labels = forms.MultipleChoiceField(required=False, choices=[])
```

- [ ] **Step 4: Populate the choices in `__init__`**

In `radis/search/forms.py`, in `SearchForm.__init__`, after the modalities
choices/size block (after line 70, before `self.query_helper = FormHelper()`),
add:

```python
        active_labels = Label.objects.filter(active=True).order_by("name")
        self.fields["labels"].choices = [  # type: ignore
            (label.name, label.name) for label in active_labels
        ]
        self.fields["labels"].widget.attrs["size"] = 6
```

- [ ] **Step 5: Conditionally add the field to the filters layout**

In `radis/search/forms.py`, in `create_filters_layout`, insert the labels field
right after the `modalities` field, guarded by whether choices exist. Replace:

```python
            Field("modalities", css_class="form-select-sm"),
            Field("study_date_from", css_class="form-control-sm"),
```

with:

```python
            Field("modalities", css_class="form-select-sm"),
            *(
                [Field("labels", css_class="form-select-sm")]
                if self.fields["labels"].choices
                else []
            ),
            Field("study_date_from", css_class="form-control-sm"),
```

(`create_filters_layout` runs at the end of `__init__`, after the choices are
set, so `self.fields["labels"].choices` is already populated here.)

- [ ] **Step 6: Run the form tests to verify they pass**

Run: `uv run cli test -- radis/search/tests/test_forms.py -v`
Expected: PASS for all four tests.

---

### Task 3: Thread selected labels through the view and remove the typed syntax

**Files:**
- Modify: `radis/search/views.py`
- Modify: `radis/search/tests/test_views.py` (add a test)
- Modify: `radis/labels/tests/test_search_filter.py` (remove the old view test)
- Delete: `radis/labels/query.py`
- Delete: `radis/labels/tests/unit/test_query.py`

- [ ] **Step 1: Write the failing view test**

In `radis/search/tests/test_views.py`, add this test (the helpers
`create_test_user_with_active_group` already exist in that file):

```python
@pytest.mark.django_db
def test_search_view_threads_selected_labels_into_filters(client: Client) -> None:
    """Selected labels from the form are passed through to SearchFilters."""
    from radis.labels.factories import LabelFactory
    from radis.search.site import Search, SearchProvider, SearchResult

    LabelFactory.create(name="edema", active=True)
    LabelFactory.create(name="pneumonia", active=True)

    user = create_test_user_with_active_group()
    client.force_login(user)

    captured: dict[str, Search] = {}

    def capturing_search(search: Search) -> SearchResult:
        captured["search"] = search
        return SearchResult(total_count=0, total_relation="exact", documents=[])

    provider = SearchProvider(name="Capturing", search=capturing_search, max_results=1000)

    with patch("radis.search.views.search_provider", provider):
        response = client.get("/search/", {"query": "chest", "labels": ["edema", "pneumonia"]})

    assert response.status_code == 200
    assert captured["search"].filters.labels == ["edema", "pneumonia"]
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run cli test -- radis/search/tests/test_views.py::test_search_view_threads_selected_labels_into_filters -v`
Expected: FAIL — the view ignores the `labels` POST/GET field and currently
derives labels only from `extract_label_filters(query)`, so
`filters.labels` is `[]`.

- [ ] **Step 3: Update the view to read the form field**

In `radis/search/views.py`, remove the import on line 11:

```python
from radis.labels.query import extract_label_filters
```

Add a read of the cleaned `labels` field alongside the other
`form.cleaned_data[...]` reads (after line 39, `age_till = ...`):

```python
        labels = form.cleaned_data["labels"]
```

Remove the label-extraction line (line 60):

```python
        remaining_query, label_names = extract_label_filters(query)
```

Change the parser call (line 62) to parse the full query directly:

```python
        query_node, fixes = QueryParser().parse(query)
```

In the `SearchFilters(...)` construction, change `labels=label_names` (line 80)
to:

```python
                    labels=labels,
```

- [ ] **Step 4: Run the view test to verify it passes**

Run: `uv run cli test -- radis/search/tests/test_views.py::test_search_view_threads_selected_labels_into_filters -v`
Expected: PASS.

- [ ] **Step 5: Remove the obsolete typed-syntax view test**

In `radis/labels/tests/test_search_filter.py`, delete the entire
`test_search_view_extracts_label_filter_from_query` function (the last test in
the file). Then remove the now-unused imports at the top of that file:

```python
from unittest.mock import patch
```
```python
from adit_radis_shared.accounts.factories import GroupFactory, UserFactory
from django.test import Client
```
```python
from radis.search.site import Search, SearchFilters, SearchProvider, SearchResult
```
```python
from radis.search.utils.query_parser import QueryParser
```

Replace the search.site import with only what the remaining provider tests use:

```python
from radis.search.site import SearchFilters
```

(The remaining tests only reference `SearchFilters`, `_build_filter_query`,
`LabelResult`, `ReportSearchVector`, `ReportFactory`, `Language`,
`LabelFactory`, `LabelResultFactory`.)

- [ ] **Step 6: Delete the dead parser module and its unit test**

Run:

```bash
rm radis/labels/query.py radis/labels/tests/unit/test_query.py
```

- [ ] **Step 7: Verify nothing else imports the deleted module**

Run: `grep -rn "labels.query\|extract_label_filters" radis/`
Expected: no output (no remaining references).

- [ ] **Step 8: Run the affected test files to verify they pass**

Run: `uv run cli test -- radis/labels/tests/test_search_filter.py radis/search/tests/test_views.py radis/search/tests/test_forms.py -v`
Expected: PASS, with no collection error for the deleted `test_query.py`.

---

### Task 4: Update documentation

**Files:**
- Modify: `CLAUDE.md`

- [ ] **Step 1: Update the labels overview reference**

In `CLAUDE.md`, in the `radis.labels/` bullet under "Django Apps", change the
phrase describing surfacing buckets so it no longer implies a typed `label:`
filter. Replace:

```
the three surfacing buckets drive report-detail badges and the `label:` search filter.
```

with:

```
the three surfacing buckets drive report-detail badges and the label filter in the search Filters panel.
```

- [ ] **Step 2: Run the full labels + search test suites as a final check**

Run: `uv run cli test -- radis/labels radis/search -v`
Expected: PASS (no failures, no errors from removed modules).

- [ ] **Step 3: Stop and report**

Do NOT commit. Report the changed/deleted files and the test results to the
user so they can review and commit themselves.

---

## Notes for the implementer

- **Why a subquery for the OR clause:** filtering across the `label_results`
  reverse relation can produce duplicate report rows; selecting distinct report
  PKs via `Report.objects.filter(...).values("pk")` and using
  `Q(report__in=...)` keeps the result set clean.
- **Surfacing buckets only:** `LabelResult.SURFACING_VALUES` is
  `(PRESENT, LIKELY, POSSIBLE)`. `ABSENT`/`UNMENTIONED` must never match — the
  provider tests assert this.
- **Label names are globally unique** (`unique_label_name` constraint), so a
  flat `(name, name)` choice list is unambiguous; no group prefixing needed.
