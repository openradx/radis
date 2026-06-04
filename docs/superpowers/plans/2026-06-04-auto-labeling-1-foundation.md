# Auto-Labeling — Plan 1: Foundation + Core Engine Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stand up the `radis.labels` Django app with its data model, dynamic LLM schema/prompt builders, and the `label_report()` core function — the engine that classifies one report against admin-defined labels, callable directly from the Django shell.

**Architecture:** A new Django app holds five models (`LabelGroup`, `Label`, `LabelResult`, `GateAnswer`, `LabelingScanCheckpoint`). The labeling engine is a pure function `label_report(report_id)` that runs a two-phase gate-then-label flow against the existing `ChatClient` (reused from `radis.chats`). Structured-output schemas are generated at runtime from DB rows via `pydantic.create_model`; prompts are static and generic, carrying only the report body. No Job/Task machinery, admin, scan, or UI in this plan — those are Plans 2 and 3.

**Tech Stack:** Python 3.12, Django 5.1, PostgreSQL 17, Pydantic v2, pytest / pytest-django, factory-boy, OpenAI-compatible `ChatClient`.

**Source spec:** `docs/superpowers/specs/2026-05-21-auto-labeling-design.md`

---

## Roadmap (this feature spans three plans)

This document is **Plan 1 of 3**. The auto-labeling feature is decomposed into three sequential, individually-shippable plans:

1. **Plan 1 (this doc) — Foundation + Core Engine.** Models, migrations, settings, schema/prompt builders, `label_report()`. End state: `label_report(id)` works from the shell and writes `LabelResult`/`GateAnswer` rows.
2. **Plan 2 — Execution Paths + Admin.** `LabelingJob`/`LabelingTask` (subclassing `AnalysisJob`/`AnalysisTask`), `process_labeling_job`/`process_labeling_task`, the `_needs_work_queryset` backfill scope, the periodic `incremental_label_scan` + checkpoint advancement, and the admin cockpit. End state: labeling runs automatically on a cron and via an admin-triggered backfill.
3. **Plan 3 — Surfacing + Observability.** Report-detail badge component, `label:` search filter + facet panel (net-new parser field-filter infrastructure), `labels-status` management command, and docs (`CLAUDE.md`, `KNOWLEDGE.md`, `example.env`). End state: end-users see and filter by labels.

Each plan gets its own `writing-plans` pass. Do not start Plan 2/3 work from this document.

### Known reconciliations with the codebase (carried into Plan 2)

These are real gaps between the spec's illustrative code and the actual base classes — they belong to Plan 2 but are recorded here so they are not lost:

- `AnalysisJob.owner` is a **non-nullable** FK to the user model. Scan-created `LabelingJob`s have no human owner — Plan 2 must provide a system owner (or make `owner` nullable on `LabelingJob`).
- `ExtractionJob` declares its own `queued_job` `OneToOneField`; the abstract `AnalysisJob` does **not**. `LabelingJob.delay()` relies on `queued_job_id`, so Plan 2 must declare that field on `LabelingJob`.
- The real `process_extraction_job` enforces a strict PREPARING→PENDING invariant (never enqueue tasks while PREPARING). Plan 2's `process_labeling_job` must mirror it.

---

## File Structure (Plan 1)

**Create:**
- `radis/labels/__init__.py` — empty package marker.
- `radis/labels/apps.py` — `LabelsConfig` AppConfig.
- `radis/labels/models.py` — the five models + stale-detection helpers.
- `radis/labels/factories.py` — factory-boy factories for tests.
- `radis/labels/utils/__init__.py` — empty package marker.
- `radis/labels/utils/schemas.py` — `BucketValue`, `GateValue`, schema builders, parsers.
- `radis/labels/utils/prompts.py` — `render_label_prompt`, `render_gate_prompt`.
- `radis/labels/labeling.py` — `label_report` + `_run_label_set` + `_get_stale_or_missing_labels`.
- `radis/labels/migrations/__init__.py` + generated `0001_initial.py`.
- `radis/labels/tests/__init__.py`, `radis/labels/tests/unit/__init__.py`.
- `radis/labels/tests/helpers.py` — `FakeChatClient` test double.
- `radis/labels/tests/unit/test_schemas.py`
- `radis/labels/tests/unit/test_prompts.py`
- `radis/labels/tests/test_models.py`
- `radis/labels/tests/test_stale_detection.py`
- `radis/labels/tests/test_labeling.py`

**Modify:**
- `radis/settings/base.py` — add the labeling settings block + default prompt constants.
- `radis/settings/base.py` `INSTALLED_APPS` — register `radis.labels.apps.LabelsConfig`.

---

## Task 1: Scaffold the `radis.labels` app and settings

**Files:**
- Create: `radis/labels/__init__.py`, `radis/labels/apps.py`, `radis/labels/utils/__init__.py`, `radis/labels/migrations/__init__.py`, `radis/labels/tests/__init__.py`, `radis/labels/tests/unit/__init__.py`
- Modify: `radis/settings/base.py` (INSTALLED_APPS + settings block)

- [ ] **Step 1: Create the package markers and AppConfig**

Create `radis/labels/__init__.py` (empty), `radis/labels/utils/__init__.py` (empty), `radis/labels/migrations/__init__.py` (empty), `radis/labels/tests/__init__.py` (empty), `radis/labels/tests/unit/__init__.py` (empty).

Create `radis/labels/apps.py`:

```python
from django.apps import AppConfig


class LabelsConfig(AppConfig):
    name = "radis.labels"
    verbose_name = "Labels"
```

- [ ] **Step 2: Register the app in INSTALLED_APPS**

In `radis/settings/base.py`, find the `radis.*` app block in `INSTALLED_APPS` (around line 82-92) and add `radis.labels` after `radis.extractions`:

```python
    "radis.extractions.apps.ExtractionsConfig",
    "radis.labels.apps.LabelsConfig",
    "radis.subscriptions.apps.SubscriptionsConfig",
```

- [ ] **Step 3: Add default prompt constants + settings block**

In `radis/settings/base.py`, after the `EXTRACTION_*` / `SUBSCRIPTION_*` settings block (after line ~440), add:

```python
# Labeling (radis.labels)
# Generic labeling system prompt. Carries NO label-specific text — each label is a field
# in the dynamically generated schema and its name/description rides in that field's
# description=. Only $report is substituted.
DEFAULT_LABELING_SYSTEM_PROMPT = """
You are an AI medical assistant. The provided schema lists one field per label, each
field's description defining the label. For every field, decide how strongly the report
below supports that label, choosing exactly one of:
  - "PRESENT"     — the report clearly states this is present
  - "LIKELY"      — the report strongly suggests it, without stating it outright
  - "POSSIBLE"    — the report leaves it as a possibility / cannot be excluded
  - "ABSENT"      — the report explicitly states this is not present
  - "UNMENTIONED" — the report does not address this at all

Return answers in JSON format matching the provided schema.

Radiology Report:
$report
"""

# Generic gate (Yes/No/Maybe applicability) system prompt. Only $report is substituted.
DEFAULT_GATE_SYSTEM_PROMPT = """
You are an AI medical assistant. The provided schema lists one field per topic, each
field's description stating the topic's screening question. For every field, answer
whether the radiology report below contains content relevant to that topic, responding
with exactly one of:
  - "YES"   — the report clearly contains relevant content
  - "NO"    — the report clearly does not
  - "MAYBE" — the report may contain relevant content; use when uncertain

Return answers in JSON format matching the provided schema.

Radiology Report:
$report
"""

LABELING_SYSTEM_PROMPT = env.str("LABELING_SYSTEM_PROMPT", default=DEFAULT_LABELING_SYSTEM_PROMPT)
LABELING_GATE_SYSTEM_PROMPT = env.str(
    "LABELING_GATE_SYSTEM_PROMPT", default=DEFAULT_GATE_SYSTEM_PROMPT
)

# Scan and manual backfill share one priority (only one LabelingJob runs at a time).
LABELING_JOB_PRIORITY = env.int("LABELING_JOB_PRIORITY", default=1)

LABELING_TASK_BATCH_SIZE = env.int("LABELING_TASK_BATCH_SIZE", default=100)
LABELING_LLM_CONCURRENCY_LIMIT = env.int("LABELING_LLM_CONCURRENCY_LIMIT", default=6)
LABELING_GATE_BATCH_SIZE = env.int("LABELING_GATE_BATCH_SIZE", default=10)

# Cron schedule for the periodic incremental scan (default: daily at 2 AM). Used in Plan 2.
LABELING_SCAN_CRON = env.str("LABELING_SCAN_CRON", default="0 2 * * *")
```

> Note: `LABELING_JOB_PRIORITY` and `LABELING_SCAN_CRON` are declared now (additive, harmless) but only consumed in Plan 2.

- [ ] **Step 4: Verify Django sees the app**

Run: `uv run cli shell -- -c "from django.apps import apps; print(apps.get_app_config('labels').verbose_name)"`
Expected: prints `Labels` with no import errors.

> If `cli shell -- -c` is not supported in this environment, instead run `uv run python -c "import django, os; os.environ.setdefault('DJANGO_SETTINGS_MODULE','radis.settings.development'); django.setup(); from django.apps import apps; print(apps.get_app_config('labels').verbose_name)"`.

- [ ] **Step 5: Commit**

```bash
git add radis/labels radis/settings/base.py
git commit -m "feat(labels): scaffold radis.labels app and settings"
```

---

## Task 2: Models + migration

**Files:**
- Create: `radis/labels/models.py`
- Create: `radis/labels/migrations/0001_initial.py` (generated)
- Test: `radis/labels/tests/test_models.py`

- [ ] **Step 1: Write the model definitions**

Create `radis/labels/models.py`:

```python
from django.db import models

from radis.reports.models import Report


class LabelGroup(models.Model):
    name = models.CharField(max_length=100, unique=True)
    gate_question = models.TextField()  # upfront Yes/No/Maybe screening question for this group
    updated_at = models.DateTimeField(auto_now=True)  # drives gate stale detection

    labels: models.QuerySet["Label"]
    gate_answers: models.QuerySet["GateAnswer"]

    class Meta:
        ordering = ["name"]

    def __str__(self) -> str:
        return f"LabelGroup {self.name} [{self.pk}]"


class Label(models.Model):
    group = models.ForeignKey(LabelGroup, on_delete=models.CASCADE, related_name="labels")
    name = models.CharField(max_length=100)  # the label string that surfaces (e.g. "pneumonia")
    description = models.TextField()  # definition sent to the LLM to classify this label
    active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)  # drives result stale detection

    results: models.QuerySet["LabelResult"]

    class Meta:
        constraints = [models.UniqueConstraint(fields=["name"], name="unique_label_name")]
        indexes = [models.Index(fields=["active"])]

    def __str__(self) -> str:
        return f"Label {self.name} [{self.pk}]"


class LabelResult(models.Model):
    class Value(models.TextChoices):
        PRESENT = "PRESENT", "Present"
        LIKELY = "LIKELY", "Likely"
        POSSIBLE = "POSSIBLE", "Possible"
        ABSENT = "ABSENT", "Absent"
        UNMENTIONED = "UNMENTIONED", "Unmentioned"

    # Buckets that attach the label to the report / search.
    SURFACING_VALUES = (Value.PRESENT, Value.LIKELY, Value.POSSIBLE)

    report = models.ForeignKey(Report, on_delete=models.CASCADE, related_name="label_results")
    label = models.ForeignKey(Label, on_delete=models.CASCADE, related_name="results")
    value = models.CharField(max_length=11, choices=Value.choices)
    generated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["report", "label"], name="unique_result_per_report_label"
            ),
        ]
        indexes = [
            models.Index(fields=["label", "value"]),  # search facet lookups
            models.Index(fields=["report"]),  # report detail page render
        ]

    def __str__(self) -> str:
        return f"LabelResult {self.label_id}={self.value} [{self.pk}]"


class GateAnswer(models.Model):
    class Value(models.TextChoices):
        YES = "YES", "Yes"
        NO = "NO", "No"
        MAYBE = "MAYBE", "Maybe"

    report = models.ForeignKey(Report, on_delete=models.CASCADE, related_name="gate_answers")
    label_group = models.ForeignKey(
        LabelGroup, on_delete=models.CASCADE, related_name="gate_answers"
    )
    value = models.CharField(max_length=5, choices=Value.choices)
    generated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["report", "label_group"], name="unique_gate_answer_per_report_group"
            ),
        ]
        indexes = [models.Index(fields=["label_group", "value"])]

    def __str__(self) -> str:
        return f"GateAnswer {self.label_group_id}={self.value} [{self.pk}]"


class LabelingScanCheckpoint(models.Model):
    last_scanned_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = "Labeling scan checkpoint"
        constraints = [
            models.CheckConstraint(
                check=models.Q(id=1), name="singleton_labeling_scan_checkpoint"
            ),
        ]

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)

    def __str__(self) -> str:
        return f"LabelingScanCheckpoint (last_scanned_at={self.last_scanned_at})"
```

- [ ] **Step 2: Generate the migration**

Run: `uv run cli makemigrations labels` (or `uv run python manage.py makemigrations labels` if no cli wrapper for it).
Expected: creates `radis/labels/migrations/0001_initial.py` with the five models. Inspect it to confirm the unique constraints, the check constraint on the checkpoint, and the indexes are present.

- [ ] **Step 3: Write the factories**

Create `radis/labels/factories.py`:

```python
import factory

from radis.reports.factories import ReportFactory

from .models import GateAnswer, Label, LabelGroup, LabelResult


class BaseDjangoModelFactory[T](factory.django.DjangoModelFactory):
    @classmethod
    def create(cls, *args, **kwargs) -> T:
        return super().create(*args, **kwargs)


class LabelGroupFactory(BaseDjangoModelFactory[LabelGroup]):
    class Meta:
        model = LabelGroup

    name = factory.Sequence(lambda n: f"Group {n}")
    gate_question = factory.Faker("sentence")


class LabelFactory(BaseDjangoModelFactory[Label]):
    class Meta:
        model = Label

    group = factory.SubFactory(LabelGroupFactory)
    name = factory.Sequence(lambda n: f"label-{n}")
    description = factory.Faker("sentence")
    active = True


class LabelResultFactory(BaseDjangoModelFactory[LabelResult]):
    class Meta:
        model = LabelResult

    report = factory.SubFactory(ReportFactory)
    label = factory.SubFactory(LabelFactory)
    value = LabelResult.Value.PRESENT


class GateAnswerFactory(BaseDjangoModelFactory[GateAnswer]):
    class Meta:
        model = GateAnswer

    report = factory.SubFactory(ReportFactory)
    label_group = factory.SubFactory(LabelGroupFactory)
    value = GateAnswer.Value.YES
```

- [ ] **Step 4: Write the model tests (cascade, uniqueness, upsert)**

Create `radis/labels/tests/test_models.py`:

```python
import pytest
from django.db import IntegrityError, transaction

from radis.labels.factories import (
    GateAnswerFactory,
    LabelFactory,
    LabelGroupFactory,
    LabelResultFactory,
)
from radis.labels.models import GateAnswer, Label, LabelGroup, LabelResult
from radis.reports.factories import ReportFactory


@pytest.mark.django_db
def test_group_delete_cascades_to_labels_and_gate_answers():
    group = LabelGroupFactory.create()
    label = LabelFactory.create(group=group)
    GateAnswerFactory.create(label_group=group)

    group.delete()

    assert not Label.objects.filter(pk=label.pk).exists()
    assert GateAnswer.objects.count() == 0


@pytest.mark.django_db
def test_label_delete_cascades_to_results():
    label = LabelFactory.create()
    result = LabelResultFactory.create(label=label)

    label.delete()

    assert not LabelResult.objects.filter(pk=result.pk).exists()


@pytest.mark.django_db
def test_label_name_is_unique():
    LabelFactory.create(name="pneumonia")
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            LabelFactory.create(name="pneumonia")


@pytest.mark.django_db
def test_group_name_is_unique():
    LabelGroupFactory.create(name="Chest")
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            LabelGroupFactory.create(name="Chest")


@pytest.mark.django_db
def test_result_unique_per_report_label_and_upsert():
    report = ReportFactory.create()
    label = LabelFactory.create()
    LabelResult.objects.create(report=report, label=label, value=LabelResult.Value.PRESENT)

    obj, created = LabelResult.objects.update_or_create(
        report=report, label=label, defaults={"value": LabelResult.Value.ABSENT}
    )
    assert created is False
    assert obj.value == LabelResult.Value.ABSENT
    assert LabelResult.objects.filter(report=report, label=label).count() == 1


@pytest.mark.django_db
def test_gate_answer_unique_per_report_group_and_upsert():
    report = ReportFactory.create()
    group = LabelGroupFactory.create()
    GateAnswer.objects.create(report=report, label_group=group, value=GateAnswer.Value.YES)

    obj, created = GateAnswer.objects.update_or_create(
        report=report, label_group=group, defaults={"value": GateAnswer.Value.NO}
    )
    assert created is False
    assert obj.value == GateAnswer.Value.NO
    assert GateAnswer.objects.filter(report=report, label_group=group).count() == 1


@pytest.mark.django_db
def test_scan_checkpoint_is_singleton():
    from radis.labels.models import LabelingScanCheckpoint

    LabelingScanCheckpoint.objects.create()
    LabelingScanCheckpoint.objects.create()  # save() forces pk=1, so this overwrites row 1

    assert LabelingScanCheckpoint.objects.count() == 1
```

- [ ] **Step 5: Run the model tests**

Run: `uv run cli test -- radis/labels/tests/test_models.py -v`
Expected: all tests PASS (migration applied to the test DB automatically).

- [ ] **Step 6: Commit**

```bash
git add radis/labels/models.py radis/labels/migrations radis/labels/factories.py radis/labels/tests/test_models.py
git commit -m "feat(labels): add data model, migration, and factories"
```

---

## Task 3: Stale-detection query tests

The spec defines two stale predicates: a `LabelResult` is stale when `generated_at < label.updated_at`; a `GateAnswer` is stale when `generated_at < label_group.updated_at`. These are pure joins over indexed columns — this task pins them down with tests so later code (Plan 2 backfill scope, admin `is_stale`) can rely on the exact semantics.

**Files:**
- Test: `radis/labels/tests/test_stale_detection.py`

- [ ] **Step 1: Write the stale-detection tests**

Create `radis/labels/tests/test_stale_detection.py`:

```python
import pytest
from django.db.models import F
from django.utils import timezone

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
```

> Note on timing: `auto_now` writes a fresh timestamp on every `save()`, and a subsequent edit's `save()` happens strictly later in wall-clock time, so `result.generated_at < label.updated_at` holds after an edit. These tests do not need to manipulate timestamps manually.

- [ ] **Step 2: Run the tests**

Run: `uv run cli test -- radis/labels/tests/test_stale_detection.py -v`
Expected: all four tests PASS.

- [ ] **Step 3: Commit**

```bash
git add radis/labels/tests/test_stale_detection.py
git commit -m "test(labels): pin down result and gate stale-detection semantics"
```

---

## Task 4: Dynamic schema builders

**Files:**
- Create: `radis/labels/utils/schemas.py`
- Test: `radis/labels/tests/unit/test_schemas.py`

- [ ] **Step 1: Write the failing unit tests**

Create `radis/labels/tests/unit/test_schemas.py`:

```python
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from radis.labels.models import GateAnswer, LabelResult
from radis.labels.utils.schemas import (
    BucketValue,
    GateValue,
    build_gate_schema,
    build_label_classification_schema,
    parse_gate_results,
    parse_label_results,
)


def _label(id, name="pneumonia", description="infection of the lung"):
    return SimpleNamespace(id=id, name=name, description=description)


def _group(id, gate_question="Is this a chest study?"):
    return SimpleNamespace(id=id, gate_question=gate_question)


def test_label_schema_fields_are_id_keyed_and_carry_name_and_description():
    Schema = build_label_classification_schema([_label(42, "pneumonia", "lung infection")])
    assert "label_42" in Schema.model_fields
    field = Schema.model_fields["label_42"]
    assert "pneumonia" in field.description
    assert "lung infection" in field.description


def test_label_schema_accepts_all_five_buckets_and_rejects_unknown():
    Schema = build_label_classification_schema([_label(1)])
    for bucket in ("PRESENT", "LIKELY", "POSSIBLE", "ABSENT", "UNMENTIONED"):
        assert Schema(label_1=bucket).label_1 == bucket
    with pytest.raises(ValidationError):
        Schema(label_1="BOGUS")


def test_label_schema_field_is_required():
    Schema = build_label_classification_schema([_label(1)])
    with pytest.raises(ValidationError):
        Schema()  # missing label_1


def test_gate_schema_fields_are_id_keyed_and_carry_question():
    Schema = build_gate_schema([_group(7, "Does this report mention the lungs?")])
    assert "group_7" in Schema.model_fields
    assert "lungs" in Schema.model_fields["group_7"].description


def test_gate_schema_validates_yes_no_maybe_and_rejects_unknown():
    Schema = build_gate_schema([_group(1)])
    for value in ("YES", "NO", "MAYBE"):
        assert Schema(group_1=value).group_1 == value
    with pytest.raises(ValidationError):
        Schema(group_1="PROBABLY")


def test_parse_label_results_round_trips_ids():
    Schema = build_label_classification_schema([_label(1), _label(2, "edema", "fluid")])
    parsed = Schema(label_1="PRESENT", label_2="ABSENT")
    assert parse_label_results(parsed) == {1: "PRESENT", 2: "ABSENT"}


def test_parse_gate_results_round_trips_ids():
    Schema = build_gate_schema([_group(3), _group(4)])
    parsed = Schema(group_3="YES", group_4="NO")
    assert parse_gate_results(parsed) == {3: "YES", 4: "NO"}


def test_bucket_and_gate_enums_match_model_choices():
    """Drift guard: static enums must stay in sync with the model TextChoices."""
    assert {b.value for b in BucketValue} == {c.value for c in LabelResult.Value}
    assert {g.value for g in GateValue} == {c.value for c in GateAnswer.Value}
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run cli test -- radis/labels/tests/unit/test_schemas.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'radis.labels.utils.schemas'`.

- [ ] **Step 3: Implement the schema builders**

Create `radis/labels/utils/schemas.py`:

```python
from enum import StrEnum
from typing import Sequence

from pydantic import BaseModel, Field, create_model


class BucketValue(StrEnum):
    PRESENT = "PRESENT"
    LIKELY = "LIKELY"
    POSSIBLE = "POSSIBLE"
    ABSENT = "ABSENT"
    UNMENTIONED = "UNMENTIONED"


class GateValue(StrEnum):
    YES = "YES"
    NO = "NO"
    MAYBE = "MAYBE"


def build_label_classification_schema(labels: Sequence) -> type[BaseModel]:
    """Build a Pydantic model with one required `label_<id>` field per label.

    The enum type is static (the five buckets); only the set of fields is dynamic.
    Each label's name + description rides in the field description= so the LLM reads it
    via the JSON schema. The answer is keyed purely by id — immune to renames.
    """
    fields = {
        f"label_{lbl.id}": (BucketValue, Field(description=f"{lbl.name}: {lbl.description}"))
        for lbl in labels
    }
    return create_model("LabelClassification", **fields)


def build_gate_schema(groups: Sequence) -> type[BaseModel]:
    """Build a Pydantic model with one required `group_<id>` field per group."""
    fields = {
        f"group_{g.id}": (GateValue, Field(description=g.gate_question)) for g in groups
    }
    return create_model("GateScreening", **fields)


def parse_label_results(parsed: BaseModel) -> dict[int, str]:
    return {int(k.removeprefix("label_")): str(v) for k, v in parsed.model_dump().items()}


def parse_gate_results(parsed: BaseModel) -> dict[int, str]:
    return {int(k.removeprefix("group_")): str(v) for k, v in parsed.model_dump().items()}
```

> `str(v)` normalizes the dumped value (a `StrEnum` member) to a plain string, so callers get the `dict[int, str]` the spec promises and storing into the `CharField` is unambiguous.

- [ ] **Step 4: Run to verify pass**

Run: `uv run cli test -- radis/labels/tests/unit/test_schemas.py -v`
Expected: all tests PASS.

- [ ] **Step 5: Commit**

```bash
git add radis/labels/utils/schemas.py radis/labels/tests/unit/test_schemas.py
git commit -m "feat(labels): add dynamic LLM schema builders with id-keyed fields"
```

---

## Task 5: Generic prompt builders

**Files:**
- Create: `radis/labels/utils/prompts.py`
- Test: `radis/labels/tests/unit/test_prompts.py`

- [ ] **Step 1: Write the failing unit tests**

Create `radis/labels/tests/unit/test_prompts.py`:

```python
import pytest

from radis.labels.utils.prompts import render_gate_prompt, render_label_prompt


def test_label_prompt_substitutes_report_including_unicode():
    body = "Lungen frei. 腫瘤なし. Sin patología."
    rendered = render_label_prompt(body)
    assert body in rendered


def test_label_prompt_teaches_all_five_buckets():
    rendered = render_label_prompt("x")
    for bucket in ("PRESENT", "LIKELY", "POSSIBLE", "ABSENT", "UNMENTIONED"):
        assert bucket in rendered


def test_gate_prompt_substitutes_report_and_teaches_gate_values():
    rendered = render_gate_prompt("report body here")
    assert "report body here" in rendered
    for value in ("YES", "NO", "MAYBE"):
        assert value in rendered


def test_prompts_contain_no_label_specific_text():
    """Per-label content belongs only in the schema field descriptions, never the prompt."""
    rendered = render_label_prompt("the lungs are clear")
    assert "pneumonia" not in rendered.lower()
    assert "$report" not in rendered  # placeholder must be fully substituted
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run cli test -- radis/labels/tests/unit/test_prompts.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'radis.labels.utils.prompts'`.

- [ ] **Step 3: Implement the prompt builders**

Create `radis/labels/utils/prompts.py`:

```python
from string import Template

from django.conf import settings


def render_label_prompt(report_body: str) -> str:
    return Template(settings.LABELING_SYSTEM_PROMPT).substitute(report=report_body)


def render_gate_prompt(report_body: str) -> str:
    return Template(settings.LABELING_GATE_SYSTEM_PROMPT).substitute(report=report_body)
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run cli test -- radis/labels/tests/unit/test_prompts.py -v`
Expected: all tests PASS.

- [ ] **Step 5: Commit**

```bash
git add radis/labels/utils/prompts.py radis/labels/tests/unit/test_prompts.py
git commit -m "feat(labels): add generic label and gate prompt renderers"
```

---

## Task 6: Core `label_report` function

This is the heart of the engine: the two-phase gate-then-label flow. It is driven directly in tests with a `FakeChatClient` so we can assert exact LLM call counts and stored rows.

**Files:**
- Create: `radis/labels/tests/helpers.py`
- Create: `radis/labels/labeling.py`
- Test: `radis/labels/tests/test_labeling.py`

- [ ] **Step 1: Write the FakeChatClient test double**

Create `radis/labels/tests/helpers.py`:

```python
"""Test double for ChatClient used by label_report.

extract_data inspects the dynamically-built schema's field names (`group_<id>` or
`label_<id>`) and returns a valid instance populated from the configured answer maps.
Recorded calls let tests assert exact gate vs. label LLM call counts.
"""
from pydantic import BaseModel


class FakeChatClient:
    def __init__(self, gate_values: dict[int, str] | None = None,
                 label_values: dict[int, str] | None = None) -> None:
        self.gate_values = gate_values or {}
        self.label_values = label_values or {}
        self.gate_calls: list[list[int]] = []
        self.label_calls: list[list[int]] = []

    def extract_data(self, prompt: str, schema: type[BaseModel]) -> BaseModel:
        data: dict[str, str] = {}
        field_names = list(schema.model_fields.keys())
        if field_names and field_names[0].startswith("group_"):
            ids = [int(n.removeprefix("group_")) for n in field_names]
            self.gate_calls.append(ids)
            for n, gid in zip(field_names, ids):
                data[n] = self.gate_values[gid]
        else:
            ids = [int(n.removeprefix("label_")) for n in field_names]
            self.label_calls.append(ids)
            for n, lid in zip(field_names, ids):
                data[n] = self.label_values[lid]
        return schema(**data)
```

- [ ] **Step 2: Write the failing tests for `label_report`**

Create `radis/labels/tests/test_labeling.py`:

```python
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
        label_report(report.id)

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
        label_report(report.id)

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
        label_report(report.id)

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
        label_report(report.id)

    # All buckets are stored (ABSENT/UNMENTIONED produce real rows).
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
    # All gates NO so no label calls; we only assert gate batching here.
    client = FakeChatClient(gate_values={g.id: "NO" for g in groups})
    with _patch_client(client):
        label_report(report.id)

    assert len(client.gate_calls) == 2
    assert [len(c) for c in client.gate_calls] == [10, 10]


@pytest.mark.django_db
def test_fresh_gate_and_fresh_results_make_zero_llm_calls():
    from radis.labels.labeling import label_report

    report = ReportFactory.create(body="lungs clear")
    group = LabelGroupFactory.create()
    label = LabelFactory.create(group=group)
    # Pre-seed a fresh YES gate and a fresh result.
    GateAnswerFactory.create(report=report, label_group=group, value="YES")
    LabelResult.objects.create(report=report, label=label, value=LabelResult.Value.PRESENT)

    client = FakeChatClient()  # any call would KeyError → proves no calls happen
    with _patch_client(client):
        label_report(report.id)

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
    # Edit stale_label so its result becomes stale.
    stale_label.description = "edited"
    stale_label.save()

    client = FakeChatClient(label_values={stale_label.id: "ABSENT"})
    with _patch_client(client):
        label_report(report.id)

    assert client.gate_calls == []  # gate still fresh
    assert client.label_calls == [[stale_label.id]]  # only the stale label
    assert LabelResult.objects.get(report=report, label=stale_label).value == "ABSENT"


@pytest.mark.django_db
def test_gate_flip_yes_to_no_deletes_results_atomically():
    from radis.labels.labeling import label_report

    report = ReportFactory.create(body="lungs clear")
    group = LabelGroupFactory.create()
    label = LabelFactory.create(group=group)
    # Pre-seed a stale YES gate with an existing result.
    GateAnswerFactory.create(report=report, label_group=group, value="YES")
    LabelResult.objects.create(report=report, label=label, value=LabelResult.Value.PRESENT)
    group.gate_question = "changed?"  # makes the gate stale → re-evaluated
    group.save()

    client = FakeChatClient(gate_values={group.id: "NO"})
    with _patch_client(client):
        label_report(report.id)

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
        label_report(report.id)

    assert len(client.gate_calls) == 1
    assert client.label_calls == [[label.id]]
    assert LabelResult.objects.get(report=report, label=label).value == "PRESENT"
```

- [ ] **Step 3: Run to verify failure**

Run: `uv run cli test -- radis/labels/tests/test_labeling.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'radis.labels.labeling'`.

- [ ] **Step 4: Implement `label_report` and helpers**

Create `radis/labels/labeling.py`:

```python
import logging
from itertools import batched

from django.conf import settings
from django.db import transaction
from django.db.models import F

from radis.chats.utils.chat_client import ChatClient
from radis.reports.models import Report

from .models import GateAnswer, Label, LabelGroup, LabelResult
from .utils.prompts import render_gate_prompt, render_label_prompt
from .utils.schemas import (
    build_gate_schema,
    build_label_classification_schema,
    parse_gate_results,
    parse_label_results,
)

logger = logging.getLogger(__name__)


def label_report(report_id: int) -> None:
    """Classify one report against all active label groups using the gate-then-label flow.

    The single function used by both execution paths (Plan 2). Nothing in its control flow
    branches on a label's bucket value — the LLM returns a bucket per label and it is stored
    as-is.
    """
    report = Report.objects.get(id=report_id)
    if not report.body or not report.body.strip():
        logger.warning("Report %s has empty body, skipping labeling.", report_id)
        return

    active_groups = list(
        LabelGroup.objects.filter(labels__active=True).prefetch_related("labels").distinct()
    )
    if not active_groups:
        logger.warning("No active label groups, skipping labeling of report %s.", report_id)
        return

    client = ChatClient()

    existing_gates = {
        ga.label_group_id: ga
        for ga in GateAnswer.objects.filter(report=report, label_group__in=active_groups)
    }

    groups_needing_gate = [
        g
        for g in active_groups
        if g.id not in existing_gates or existing_gates[g.id].generated_at < g.updated_at
    ]
    needing_ids = {g.id for g in groups_needing_gate}
    groups_with_fresh_gate = {
        g.id: existing_gates[g.id].value for g in active_groups if g.id not in needing_ids
    }

    # Phase 1 — Gate: only for groups with stale or missing gate answers.
    new_gate_results: dict[int, str] = {}
    for gate_batch in batched(groups_needing_gate, settings.LABELING_GATE_BATCH_SIZE):
        schema = build_gate_schema(gate_batch)
        parsed = client.extract_data(render_gate_prompt(report.body), schema)
        new_gate_results.update(parse_gate_results(parsed))

    # Phase 2 — process each group.
    for group in active_groups:
        labels = [lbl for lbl in group.labels.all() if lbl.active]

        if group.id in new_gate_results:
            new_value = new_gate_results[group.id]
            old_gate = existing_gates.get(group.id)
            old_value = old_gate.value if old_gate else None

            with transaction.atomic():
                GateAnswer.objects.update_or_create(
                    report=report, label_group=group, defaults={"value": new_value}
                )
                if new_value == GateAnswer.Value.NO and old_value in (
                    GateAnswer.Value.YES,
                    GateAnswer.Value.MAYBE,
                ):
                    LabelResult.objects.filter(report=report, label__group=group).delete()

            if new_value in (GateAnswer.Value.YES, GateAnswer.Value.MAYBE):
                labels_to_run = _get_stale_or_missing_labels(report, labels)
                if labels_to_run:
                    _run_label_set(client, report, labels_to_run)
        else:
            gate_value = groups_with_fresh_gate[group.id]
            if gate_value in (GateAnswer.Value.YES, GateAnswer.Value.MAYBE):
                labels_to_run = _get_stale_or_missing_labels(report, labels)
                if labels_to_run:
                    _run_label_set(client, report, labels_to_run)
            # else: gate = NO, fresh — skip group entirely.


def _run_label_set(client: ChatClient, report: Report, labels: list[Label]) -> None:
    schema = build_label_classification_schema(labels)
    parsed = client.extract_data(render_label_prompt(report.body), schema)
    for label_id, bucket in parse_label_results(parsed).items():
        LabelResult.objects.update_or_create(
            report=report, label_id=label_id, defaults={"value": bucket}
        )


def _get_stale_or_missing_labels(report: Report, labels: list[Label]) -> list[Label]:
    """Return labels whose LabelResult is missing or stale (result.generated_at < label.updated_at).

    One query answers both "should we run?" (non-empty) and "what to run?" (the list).
    A label that previously came back ABSENT/UNMENTIONED still has a fresh row → excluded.
    """
    fresh_ids = set(
        LabelResult.objects.filter(
            report=report,
            label_id__in=[lbl.id for lbl in labels],
            generated_at__gte=F("label__updated_at"),
        ).values_list("label_id", flat=True)
    )
    return [lbl for lbl in labels if lbl.id not in fresh_ids]
```

- [ ] **Step 5: Run to verify pass**

Run: `uv run cli test -- radis/labels/tests/test_labeling.py -v`
Expected: all tests PASS.

> `test_gate_batching_two_calls_for_twenty_groups` uses Django's `@override_settings` to pin the gate batch size (the default is also 10, so the override just makes the intent explicit and robust to config changes).

- [ ] **Step 6: Commit**

```bash
git add radis/labels/labeling.py radis/labels/tests/helpers.py radis/labels/tests/test_labeling.py
git commit -m "feat(labels): add core label_report gate-then-label engine"
```

---

## Task 7: Full-suite run, lint, and type-check

**Files:** none (verification only)

- [ ] **Step 1: Run the whole labels test suite**

Run: `uv run cli test -- radis/labels/ -v`
Expected: every test from Tasks 2–6 PASSES.

- [ ] **Step 2: Lint**

Run: `uv run cli lint`
Expected: no errors in `radis/labels/`. Fix any ruff findings (import order, line length ≤ 100) inline.

- [ ] **Step 3: Format**

Run: `uv run cli format-code`
Expected: no changes, or only auto-applied formatting. Re-run the suite if anything reformatted.

- [ ] **Step 4: Commit any lint/format fixups**

```bash
git add -A
git commit -m "chore(labels): lint and format Plan 1 foundation" || echo "nothing to commit"
```

---

## Plan 1 Definition of Done

- `radis.labels` is a registered Django app with a single initial migration.
- The five models exist with their constraints and indexes; cascade, uniqueness, and upsert behavior are tested.
- Stale-detection semantics for results and gate answers are tested.
- Schema builders produce id-keyed Pydantic models; prompt renderers are generic; a drift guard ties the static enums to the model choices.
- `label_report(report_id)` runs the full gate-then-label flow — gate batching, NO-skip, all-bucket storage, fresh/stale short-circuits, and atomic YES→NO result deletion — all covered by tests with a `FakeChatClient`.
- `uv run cli test -- radis/labels/`, `uv run cli lint` pass.

**Manual smoke check (optional, requires a configured LLM):** in `uv run cli shell`, create a `LabelGroup` + active `Label`, a `Report` with a body, call `from radis.labels.labeling import label_report; label_report(report.id)`, and confirm `GateAnswer` / `LabelResult` rows appear.

**Next:** run `writing-plans` for **Plan 2 — Execution Paths + Admin**, carrying forward the three reconciliations noted in the Roadmap section.
