# Label Search Filter — Design

**Date:** 2026-06-10
**Status:** Approved (design)
**App:** `radis.search`, `radis.pgsearch`, `radis.labels`

## Summary

Let users filter search results by selecting one or more active labels from a
multi-select listbox in the search page's existing "Filters" panel — mirroring
the modalities filter. The typed `label:<name>` query syntax is removed in
favour of this widget. All label and gate management stays in Django admin; no
new management or job-progress web UI is introduced.

## Motivation

The auto-labeling feature surfaces labels as badges on report detail pages and
supports filtering via a `label:<name>` token typed into the search query box.
Typing the prefix is awkward and undiscoverable. Surfacing the available labels
as a selectable widget — alongside the modalities filter users already know —
makes label filtering obvious and removes the need to remember label names.

## Scope

In scope:

- A `labels` multi-select widget in the search Filters panel.
- Switching the multi-label combine semantics from AND to OR.
- Removing the typed `label:<name>` query syntax and its parser.

Explicitly out of scope (YAGNI):

- Web-based label / gate-question management (stays in Django admin).
- Job/task progress web pages (admins use Django admin and `labels_status`).
- Clickable label badges on report detail.
- Grouping labels by `LabelGroup` in the filter (flat alphabetical list).

## Decisions

| Decision | Choice | Rationale |
| --- | --- | --- |
| Widget style | Multi-select listbox (`size=6`) | Mirrors the existing modalities filter; least new code, consistent UI. |
| Which labels listed | Active labels only (`active=True`) | Inactive labels stop getting new results; hiding them keeps the list clean. |
| Ordering | Alphabetical by `name` | Label `name` is globally unique, so a flat list is unambiguous. |
| Combine semantics | OR (any selected label surfaces) | Matches the modalities filter (`__in`). Changes the prior AND behavior. |
| Typed `label:` syntax | Removed | The widget is the single, discoverable way to filter by label. |

## Design

### 1. Form — `radis/search/forms.py`

Add a `labels` field to `SearchForm`:

```python
labels = forms.MultipleChoiceField(required=False, choices=[])
```

Populate choices in `__init__`, after the modalities setup:

```python
from radis.labels.models import Label

active_labels = Label.objects.filter(active=True).order_by("name")
self.fields["labels"].choices = [(label.name, label.name) for label in active_labels]
self.fields["labels"].widget.attrs["size"] = 6
```

In `create_filters_layout`, add the field directly under modalities — but only
when at least one active label exists, to avoid rendering an empty listbox:

```python
Field("modalities", css_class="form-select-sm"),
# labels field inserted here only when active labels exist
```

Because the layout is built per-request in `__init__`, conditionally include the
`Field("labels", ...)` based on whether `active_labels` is non-empty.

### 2. View — `radis/search/views.py`

- Remove the `from radis.labels.query import extract_label_filters` import and
  the query-stripping step. The full `query` goes straight to `QueryParser`.
- Read the selected labels from the form and thread them into the filters:

```python
labels = form.cleaned_data["labels"]
...
filters=SearchFilters(
    ...
    labels=labels,
),
```

### 3. Provider — `radis/pgsearch/providers.py` (AND → OR)

Replace the per-label AND loop with a single OR-style filter:

```python
if filters.labels:
    from radis.labels.models import LabelResult
    from radis.reports.models import Report

    matching = Report.objects.filter(
        label_results__label__name__in=filters.labels,
        label_results__value__in=LabelResult.SURFACING_VALUES,
    ).values("pk")
    fq &= Q(report__in=matching)
```

Using a `Report.objects.filter(...).values("pk")` subquery keeps the result set
deduplicated (no duplicate rows from the `label_results` join).

### 4. Filters docstring — `radis/search/site.py`

Update the `SearchFilters.labels` docstring: change "ALL of the given label
names" to "ANY of the given label names".

### 5. Cleanup

- Delete `radis/labels/query.py`.
- Delete `radis/labels/tests/unit/test_query.py`.
- Rewrite `radis/labels/tests/test_search_filter.py` to drive filtering through
  the form's `labels` field and assert OR semantics.

## Data Flow

1. `SearchForm.__init__` queries active labels and offers them as listbox choices.
2. User selects one or more labels (and any other filters) and submits the form.
3. `SearchView` reads `form.cleaned_data["labels"]` and builds `SearchFilters`.
4. `_build_filter_query` adds a single `Q(report__in=<surfacing subquery>)`
   matching reports that surface ANY selected label.
5. Results render as today; label badges on report detail are unchanged.

## Error Handling / Edge Cases

- **No active labels:** the `labels` field is omitted from the layout, so the
  panel shows no empty listbox.
- **No selection:** `labels` is an empty list; no label filtering is applied.
- **Stale/inactive results:** filtering matches only surfacing buckets
  (`PRESENT`/`LIKELY`/`POSSIBLE`) regardless of staleness, matching current
  behavior. Results for labels later deactivated are simply not selectable.
- **Removed typed syntax:** a literal `label:foo` typed into the query box is now
  treated as ordinary search text (parsed by `QueryParser`), no longer a filter.

## Testing (TDD)

Form (`radis/search/tests`):

- `labels` choices contain only active labels, ordered alphabetically.
- `labels` is optional (form valid with no selection).
- The `labels` field is absent from the layout when no active labels exist.

View / provider (`radis/labels/tests/test_search_filter.py`, rewritten):

- Selecting a single label returns reports that surface it.
- Selecting multiple labels returns reports surfacing ANY of them (OR).
- Only surfacing buckets (`PRESENT`/`LIKELY`/`POSSIBLE`) match; `ABSENT`/
  `UNMENTIONED` do not.
- No `labels` selection applies no label filtering.

Removed:

- `radis/labels/tests/unit/test_query.py` (parser no longer exists).

## Documentation

Update `CLAUDE.md` "Labels Not Appearing" / labels overview where it references
the `label:` search filter, to describe the Filters-panel widget instead.
