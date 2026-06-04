# Auto-Labeling — Plan 3: Surfacing + Observability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make labels visible and useful to end users — badges on the report detail page, a `label:` search filter with a facet panel — and give operators a `labels-status` command, plus the documentation updates that complete the feature.

**Architecture:** A small parser utility extracts `label:<name>` tokens from the raw search query string (the existing parser has no field-filter grammar), populates a new `SearchFilters.labels` list, and strips them from the free-text query. The pgsearch provider translates `labels` into a `LabelResult` join restricted to surfacing buckets (`PRESENT`/`LIKELY`/`POSSIBLE`). A Cotton component renders surfacing-bucket badges on the report detail page. A management command reports corpus-wide labeling status.

**Tech Stack:** Python 3.12, Django 6.0, PostgreSQL FTS, Django templates + Cotton + Bootstrap 5, pytest/pytest-django, Playwright (acceptance).

**Source spec:** `docs/superpowers/specs/2026-05-21-auto-labeling-design.md` (sections "End-User Surfacing", "Observability", "Documentation updates"). Plans 1 and 2 are prerequisites.

---

## Prerequisites and environment notes (carried from Plans 1–2)

- Models from Plan 1: `LabelResult` (with `SURFACING_VALUES = (PRESENT, LIKELY, POSSIBLE)`), `Label`, `LabelGroup`, `GateAnswer`, `LabelingScanCheckpoint`. Plan 2: `LabelingJob`/`LabelingTask`, scan, admin.
- **Django 6.0.1.** Tests: `DJANGO_SETTINGS_MODULE=radis.settings.development uv run pytest <path> -p no:cacheprovider -q` (standalone Postgres on `localhost:5432`; do NOT use `cli test`).
- Commits: prefix `PRE_COMMIT_ALLOW_NO_CONFIG=1`; end body with `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`.
- Standards: ruff, ruff format, **pyright basic** (incl. tests); djlint for templates (`uv run cli lint` runs ruff + djlint). Use `.pk` not `.id` on `Report` in tests.
- **Key architecture fact:** the existing search has NO in-query `field:value` syntax. Filters like `modalities` come from separate `SearchForm` fields → `SearchFilters` (`radis/search/site.py`) → `radis/pgsearch/providers.py::_build_filter_query`. The `label:` filter is therefore implemented by **pre-extracting** `label:` tokens from the query string before parsing, not by extending the parser grammar.

---

## File Structure (Plan 3)

**Create:**
- `radis/labels/query.py` — `extract_label_filters(query: str) -> tuple[str, list[str]]` (strip + collect `label:` tokens).
- `radis/labels/management/__init__.py`, `radis/labels/management/commands/__init__.py`, `radis/labels/management/commands/labels_status.py`.
- `radis/labels/templates/cotton/report_labels.html` — the badge component.
- `radis/labels/tests/test_query.py`, `test_search_filter.py`, `test_labels_status.py`.
- `radis/labels/tests/test_surfacing.py` (Playwright acceptance — optional, `@pytest.mark.acceptance`).

**Modify:**
- `radis/search/site.py` — add `labels: list[str]` to `SearchFilters`.
- `radis/search/views.py` (and/or `forms.py`) — call `extract_label_filters`, pass `labels` into `SearchFilters`.
- `radis/pgsearch/providers.py` — add the `labels` clause to `_build_filter_query`.
- `radis/reports/templates/reports/report_detail.html` — include `<c-report-labels :report="report" />`.
- `CLAUDE.md`, `KNOWLEDGE.md`, `example.env` — documentation.

---

## Task 1: `label:` token extraction utility

**Files:**
- Create: `radis/labels/query.py`
- Test: `radis/labels/tests/test_query.py`

- [ ] **Step 1: Write failing tests** `radis/labels/tests/test_query.py` (pure unit, no DB):

```python
from radis.labels.query import extract_label_filters


def test_extracts_single_label_and_strips_it():
    remaining, labels = extract_label_filters("pneumonia label:edema")
    assert labels == ["edema"]
    assert "label:" not in remaining
    assert remaining.strip() == "pneumonia"


def test_extracts_multiple_labels():
    remaining, labels = extract_label_filters("label:edema chest label:nodule")
    assert labels == ["edema", "nodule"]
    assert remaining.strip() == "chest"


def test_quoted_label_allows_spaces():
    remaining, labels = extract_label_filters('label:"pleural effusion" lung')
    assert labels == ["pleural effusion"]
    assert remaining.strip() == "lung"


def test_no_label_returns_query_unchanged():
    remaining, labels = extract_label_filters("just a normal query")
    assert labels == []
    assert remaining == "just a normal query"


def test_label_token_is_case_preserved_for_name_match():
    _, labels = extract_label_filters("label:Pneumonia")
    assert labels == ["Pneumonia"]
```

- [ ] **Step 2: Run to verify failure.**

- [ ] **Step 3: Implement** `radis/labels/query.py`:

```python
import re

# Matches  label:word  or  label:"two words"
_LABEL_RE = re.compile(r'label:(?:"([^"]+)"|(\S+))')


def extract_label_filters(query: str) -> tuple[str, list[str]]:
    """Pull `label:<name>` tokens out of a raw query string.

    Returns (remaining_query, label_names). `label:"a b"` supports spaces via quotes.
    The remaining query has the label tokens removed and surrounding whitespace collapsed.
    """
    labels: list[str] = []

    def _collect(match: re.Match) -> str:
        labels.append(match.group(1) or match.group(2))
        return " "

    remaining = _LABEL_RE.sub(_collect, query)
    remaining = re.sub(r"\s+", " ", remaining).strip()
    return remaining, labels
```

- [ ] **Step 4: Run to verify pass** (5 tests).
- [ ] **Step 5: Lint, type-check, commit**: `feat(labels): add label: query-token extraction`.

---

## Task 2: `SearchFilters.labels` + pgsearch translation

**Files:**
- Modify: `radis/search/site.py` (add `labels` field)
- Modify: `radis/pgsearch/providers.py` (`_build_filter_query` clause)
- Test: `radis/labels/tests/test_search_filter.py`

- [ ] **Step 1: Add the field.** In `radis/search/site.py`, add to the `SearchFilters` dataclass (after `created_before`):

```python
    labels: list[str] = field(default_factory=list)
```

Update the docstring to mention: "labels: only reports having a surfacing-bucket (PRESENT/LIKELY/POSSIBLE) result for ALL of the given label names."

- [ ] **Step 2: Write failing tests** `radis/labels/tests/test_search_filter.py`. These exercise the pgsearch filter directly via `ReportSearchVector` (mirror how pgsearch tests build data) — confirm a report with a surfacing-bucket result is matched and one with only ABSENT/UNMENTIONED is excluded.

```python
import pytest

from radis.labels.factories import LabelFactory, LabelResultFactory
from radis.labels.models import LabelResult
from radis.pgsearch.providers import _build_filter_query
from radis.reports.factories import ReportFactory
from radis.reports.models import Report
from radis.search.site import SearchFilters


def _matches(report, **filter_kwargs):
    fq = _build_filter_query(SearchFilters(group=0, **filter_kwargs))
    # _build_filter_query references report__<field>; apply over Report via the same relation.
    return Report.objects.filter(
        # emulate the ReportSearchVector relation by filtering Report through the built Q's
        # report__ prefix — see note in Step 4 about the relation root.
        pk=report.pk
    )


@pytest.mark.django_db
def test_label_filter_matches_surfacing_result():
    label = LabelFactory.create(name="edema")
    report = ReportFactory.create()
    LabelResultFactory.create(report=report, label=label, value=LabelResult.Value.PRESENT)

    from radis.pgsearch.providers import _build_filter_query
    from radis.pgsearch.models import ReportSearchVector
    # Build a search-vector row for the report so the provider's relation root resolves.
    ReportSearchVector.objects.get_or_create(report=report)
    fq = _build_filter_query(SearchFilters(group=0, labels=["edema"]))
    matched = ReportSearchVector.objects.filter(fq).values_list("report_id", flat=True)
    assert report.pk in list(matched)


@pytest.mark.django_db
def test_label_filter_excludes_absent_only_result():
    from radis.pgsearch.models import ReportSearchVector

    label = LabelFactory.create(name="edema")
    report = ReportFactory.create()
    LabelResultFactory.create(report=report, label=label, value=LabelResult.Value.ABSENT)
    ReportSearchVector.objects.get_or_create(report=report)

    fq = _build_filter_query(SearchFilters(group=0, labels=["edema"]))
    matched = ReportSearchVector.objects.filter(fq).values_list("report_id", flat=True)
    assert report.pk not in list(matched)
```

> Note: confirm the exact way `ReportSearchVector` relates to `Report` and whether a row must exist (`radis/pgsearch/models.py`, `signals.py`). Adjust the test setup to whatever the provider actually queries over. The assertion (surfacing matched, ABSENT excluded) is the contract.

- [ ] **Step 3: Run to verify failure.**

- [ ] **Step 4: Implement the clause.** In `radis/pgsearch/providers.py::_build_filter_query`, append before `return fq`:

```python
    if filters.labels:
        from radis.labels.models import LabelResult

        for name in filters.labels:
            fq &= Q(
                report__label_results__label__name=name,
                report__label_results__value__in=LabelResult.SURFACING_VALUES,
            )
```

> Each label name ANDs a separate clause (report must surface ALL requested labels). The relation `report__label_results` exists because Plan 1 declared `LabelResult.report` with `related_name="label_results"`. The local import avoids a hard pgsearch→labels dependency at module import time.

- [ ] **Step 5: Run to verify pass.** Adjust the test relation root per the Step 2 note until green.
- [ ] **Step 6: Lint, type-check, commit**: `feat(search): add label: surfacing-bucket filter to pgsearch`.

---

## Task 3: Wire `label:` extraction into the search view

**Files:**
- Modify: `radis/search/views.py` (and/or `forms.py` where the query → `SearchFilters` mapping happens)
- Test: `radis/labels/tests/test_search_filter.py` (append an integration-style test if the view is testable without full search infra; otherwise a focused unit test of the wiring)

- [ ] **Step 1: Locate the mapping.** Read `radis/search/views.py` to find where the cleaned `query` string and the form fields are turned into `Search(query=..., filters=SearchFilters(...))`. The free-text query is parsed by `QueryParser`.

- [ ] **Step 2: Insert extraction** before the query is parsed: call `remaining, label_names = extract_label_filters(raw_query)`, pass `remaining` to `QueryParser().parse(...)`, and set `labels=label_names` on the `SearchFilters`. Import `from radis.labels.query import extract_label_filters`.

- [ ] **Step 3: Test the wiring.** Add a test that constructs the view's filter-building path (or a thin helper extracted for testability) and asserts that an input query `"chest label:edema"` produces `SearchFilters.labels == ["edema"]` and a parsed query of `"chest"`. If the view is hard to unit-test, extract the query→filters mapping into a small pure helper in `radis/search/` and test that helper directly (a reasonable, focused refactor that improves testability of code you're touching).

- [ ] **Step 4: Run to verify pass; lint; type-check; commit**: `feat(search): extract label: filters from the query in the search view`.

---

## Task 4: Report-detail badge Cotton component

**Files:**
- Create: `radis/labels/templates/cotton/report_labels.html`
- Modify: `radis/reports/templates/reports/report_detail.html`
- Test: `radis/labels/tests/test_surfacing.py` (a render test; Playwright acceptance optional)

- [ ] **Step 1: Build the component.** Create `radis/labels/templates/cotton/report_labels.html`. It receives `report` and renders only surfacing-bucket results grouped by `label_group.name`. All three surfacing buckets render an identical badge; the exact bucket shows on hover (`title`). Stale results get a muted style. Mirror existing Cotton components (look in `radis/core/templates/cotton/` and existing badge usage for Bootstrap classes).

```html
{# props: report #}
{% load static %}
<div class="report-labels">
  {% regroup report.surfacing_label_results by label.group.name as grouped %}
  {% for group in grouped %}
    <div class="mb-1">
      <span class="text-muted small me-1">{{ group.grouper }}:</span>
      {% for result in group.list %}
        <span class="badge {% if result.is_stale %}bg-secondary opacity-50{% else %}bg-primary{% endif %}"
              title="{{ result.get_value_display }}{% if result.is_stale %} (stale){% endif %}">
          {{ result.label.name }}
        </span>
      {% endfor %}
    </div>
  {% endfor %}
</div>
```

- [ ] **Step 2: Provide the data.** The template uses `report.surfacing_label_results` and `result.is_stale`. Add a method/property on `Report` is undesirable (cross-app); instead expose this via a template-friendly accessor. Simplest: add a `@property surfacing_label_results` on `LabelResult`'s manager is overkill — instead, in the report detail **view** (`radis/reports/views.py` report detail) add to the context, OR add a `cached_property` on `Report` in `radis/reports/models.py`:

```python
# radis/reports/models.py (Report) — add:
from functools import cached_property

    @cached_property
    def surfacing_label_results(self):
        from radis.labels.models import LabelResult
        return (
            self.label_results.filter(value__in=LabelResult.SURFACING_VALUES)
            .select_related("label", "label__group")
            .order_by("label__group__name", "label__name")
        )
```

And add `is_stale` as a property on `LabelResult` (`radis/labels/models.py`):

```python
    @property
    def is_stale(self) -> bool:
        return self.generated_at < self.label.updated_at
```

(The `is_stale` property triggers a `label` query per result; `surfacing_label_results` already `select_related("label")`, so it's cheap.)

- [ ] **Step 3: Include the component** in `radis/reports/templates/reports/report_detail.html` at an appropriate spot (near the header/summary). Read the template first to place it idiomatically:

```html
<c-report-labels :report="report" />
```

- [ ] **Step 4: Write a render test** `radis/labels/tests/test_surfacing.py` (DB, no browser): render the component (or the detail view) for a report with one PRESENT, one ABSENT, and assert the PRESENT label name appears and the ABSENT one does not.

```python
import pytest
from django.template import Context, Template

from radis.labels.factories import LabelFactory, LabelGroupFactory, LabelResultFactory
from radis.labels.models import LabelResult
from radis.reports.factories import ReportFactory


@pytest.mark.django_db
def test_only_surfacing_buckets_render():
    group = LabelGroupFactory.create(name="Chest")
    present = LabelFactory.create(group=group, name="edema")
    absent = LabelFactory.create(group=group, name="nodule")
    report = ReportFactory.create()
    LabelResultFactory.create(report=report, label=present, value=LabelResult.Value.PRESENT)
    LabelResultFactory.create(report=report, label=absent, value=LabelResult.Value.ABSENT)

    tmpl = Template("{% load cotton %}<c-report-labels :report=\"report\" />")
    html = tmpl.render(Context({"report": report}))
    assert "edema" in html
    assert "nodule" not in html
```

> Confirm the Cotton load tag / template-rendering setup the project uses (check an existing component test). If Cotton components can't be rendered via raw `Template` in tests, render the detail view through the test client instead and assert against the response HTML.

- [ ] **Step 5: Optional Playwright acceptance** (`@pytest.mark.acceptance`): badges render for surfacing buckets, tooltip exposes the exact bucket, stale results show muted styling. Only if dev containers are available — otherwise note it as deferred.
- [ ] **Step 6: Lint (djlint template), type-check, commit**: `feat(labels): surface label badges on the report detail page`.

---

## Task 5: `labels-status` management command

**Files:**
- Create: `radis/labels/management/__init__.py`, `radis/labels/management/commands/__init__.py`, `radis/labels/management/commands/labels_status.py`
- Test: `radis/labels/tests/test_labels_status.py`

- [ ] **Step 1: Write failing test** `radis/labels/tests/test_labels_status.py`:

```python
import pytest
from django.core.management import call_command

from radis.labels.factories import LabelFactory, LabelGroupFactory, LabelResultFactory
from radis.labels.models import LabelResult
from radis.reports.factories import ReportFactory


@pytest.mark.django_db
def test_labels_status_reports_counts(capsys):
    group = LabelGroupFactory.create(name="Chest")
    label = LabelFactory.create(group=group, name="edema")
    report = ReportFactory.create()
    LabelResultFactory.create(report=report, label=label, value=LabelResult.Value.PRESENT)

    call_command("labels_status")
    out = capsys.readouterr().out
    assert "edema" in out
    assert "Present" in out or "PRESENT" in out
    assert "never" in out.lower() or "last scan" in out.lower()
```

- [ ] **Step 2: Run to verify failure.**

- [ ] **Step 3: Implement** `radis/labels/management/commands/labels_status.py`:

```python
from django.core.management.base import BaseCommand
from django.db.models import Count, Q, F

from radis.labels.models import (
    GateAnswer, Label, LabelGroup, LabelResult, LabelingScanCheckpoint,
)
from radis.reports.models import Report


class Command(BaseCommand):
    help = "Report corpus-wide auto-labeling status."

    def handle(self, *args, **options) -> None:
        checkpoint = LabelingScanCheckpoint.objects.filter(pk=1).first()
        last = checkpoint.last_scanned_at if checkpoint and checkpoint.last_scanned_at else "never"
        self.stdout.write(f"Last scan checkpoint: {last}")
        self.stdout.write(f"Total reports: {Report.objects.count()}")

        self.stdout.write("\nPer-label results:")
        for label in Label.objects.select_related("group").order_by("group__name", "name"):
            counts = {
                v.label: label.results.filter(value=v).count() for v in LabelResult.Value
            }
            stale = label.results.filter(generated_at__lt=F("label__updated_at")).count()
            summary = " · ".join(f"{n} {lbl}" for lbl, n in counts.items())
            self.stdout.write(f"  [{label.group.name}] {label.name}: {summary} · {stale} stale")

        self.stdout.write("\nPer-group gate answers:")
        for group in LabelGroup.objects.order_by("name"):
            gc = {v.label: group.gate_answers.filter(value=v).count() for v in GateAnswer.Value}
            gstale = group.gate_answers.filter(generated_at__lt=F("label_group__updated_at")).count()
            summary = " · ".join(f"{n} {lbl}" for lbl, n in gc.items())
            self.stdout.write(f"  {group.name}: {summary} · {gstale} stale")
```

- [ ] **Step 4: Run to verify pass.** (`Present` appears via `LabelResult.Value.PRESENT.label`.)
- [ ] **Step 5: Lint, type-check, commit**: `feat(labels): add labels-status management command`.

---

## Task 6: Documentation

**Files:**
- Modify: `CLAUDE.md`, `KNOWLEDGE.md`, `example.env`

- [ ] **Step 1: `CLAUDE.md`** — add `radis.labels` to the Django Apps list with a one-line description; correct the "Django 5.1+" line to reflect the installed major version (6.0); add the seven `LABELING_*` env vars to the Environment Variables section; add a Troubleshooting subsection ("Labels not appearing": check active labels exist, a backfill has run or the scan tick fired, gate answered YES/MAYBE, result is a surfacing bucket).

- [ ] **Step 2: `KNOWLEDGE.md`** — document: (a) prompt design (generic prompt + per-field descriptions), (b) label-authoring guidance (description must be self-contained and define the finding precisely), (c) gate-question authoring (topic-level applicability Yes/No/Maybe screen, not a specific finding), (d) the five-bucket meaning and which three surface.

- [ ] **Step 3: `example.env`** — add the seven new env vars with comments and defaults:

```dotenv
# Auto-labeling (radis.labels)
# LABELING_SYSTEM_PROMPT=...        # generic; only $report is substituted
# LABELING_GATE_SYSTEM_PROMPT=...   # generic gate (Yes/No/Maybe) prompt
LABELING_JOB_PRIORITY=1
LABELING_TASK_BATCH_SIZE=100
LABELING_LLM_CONCURRENCY_LIMIT=6
LABELING_GATE_BATCH_SIZE=10
LABELING_SCAN_CRON=0 2 * * *
```

- [ ] **Step 4: Commit**: `docs(labels): document the auto-labeling feature`.

---

## Task 7: Full-suite verification

**Files:** none (verification)

- [ ] **Step 1: Full suite** `DJANGO_SETTINGS_MODULE=radis.settings.development uv run pytest radis/labels/ radis/search/ radis/pgsearch/ -p no:cacheprovider -q` — all pass (including the existing search/pgsearch suites, to confirm the `SearchFilters` change didn't regress).
- [ ] **Step 2: Lint + format + pyright + djlint** across touched files. `uv run cli lint` (ruff + djlint) and `uv run pyright radis/labels/`. Fix inline.
- [ ] **Step 3: Commit** fixups: `chore(labels): lint/type-check Plan 3`.

---

## Plan 3 Definition of Done

- `label:<name>` (and quoted form) filters search to reports with a surfacing-bucket result for that label; ABSENT/UNMENTIONED-only reports are excluded; existing search tests still pass.
- The report detail page shows surfacing-bucket badges grouped by label group, with bucket-on-hover and muted styling for stale results; ABSENT/UNMENTIONED never render.
- `labels_status` prints checkpoint, totals, per-label 5-bucket + stale counts, and per-group gate counts.
- `CLAUDE.md`, `KNOWLEDGE.md`, `example.env` updated.
- Full suite + ruff + pyright + djlint clean.

**Deferred / out of scope (per spec):** facet panel UI polish, `label:foo:present` bucket syntax, a "stale" search modifier, manual label override, versioned history, Prometheus metrics, localized prompts. The facet *panel* (top-N label counts beside results) is described in the spec under "Search filter"; if desired in v1, add it as a follow-up task computing top-N `label.name` counts over `SURFACING_VALUES` across the filtered result set and rendering beside the results — but the core `label:` filter above is the shippable unit and the facet panel can land separately.

**After Plan 3:** the feature is functionally complete. Use `superpowers:finishing-a-development-branch` to integrate the three plans' work.
