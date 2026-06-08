# Labels: Name-Keyed Schema Fields + Gate YES/NO Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Switch dynamic Pydantic schemas from opaque ID-keyed fields (`label_42`, `group_7`) to human-readable name-keyed fields (`lbl.name`, `g.name`), and remove MAYBE from the gate enum so gate answers are strictly YES or NO.

**Architecture:** Two tightly coupled changes applied together: the schema layer (`utils/schemas.py`) drives the shape of every other change — once field names switch and MAYBE disappears, callers (`labeling.py`), the test double (`tests/helpers.py`), and all tests must update to match. The migration is re-squashed at the end since no production data exists.

**Tech Stack:** Python 3.12, Django 5.x, Pydantic v2, pytest-django, factory-boy

---

## File Map

| File | Change |
|---|---|
| `radis/labels/utils/schemas.py` | Name-keyed builders; remove `GateValue.MAYBE`; delete `parse_label_results`, `parse_gate_results` |
| `radis/labels/labeling.py` | Inline parse logic at both call sites; remove parse helper imports; collapse MAYBE comparisons |
| `radis/labels/models.py` | Remove `GateAnswer.Value.MAYBE`; `max_length` 5→3; update `gate_question` comment |
| `radis/labels/scope.py` | `value__in=[YES, MAYBE]` → `value=YES` |
| `radis/labels/tests/helpers.py` | `FakeChatClient`: switch to name-keyed dicts; detect schema type via `schema.__name__` |
| `radis/labels/tests/unit/test_schemas.py` | Update key assertions; remove parse helper tests; remove MAYBE; add `name` to `_group` helper |
| `radis/labels/tests/test_labeling.py` | Update `FakeChatClient` calls to name-keyed; update `label_calls` assertions; remove MAYBE tests |
| `radis/labels/tests/test_scope.py` | Remove/update the MAYBE-gate scope test |
| `radis/labels/tests/unit/test_prompts.py` | Remove MAYBE from gate prompt assertion |
| `radis/settings/base.py` | Remove MAYBE from `_DEFAULT_GATE_SYSTEM_PROMPT` |
| `example.env` | Update `LABELING_GATE_SYSTEM_PROMPT` comment |
| `radis/labels/migrations/0001_initial.py` | Delete and regenerate via `makemigrations` |

---

## Task 1: Rewrite `utils/schemas.py`

**Files:**
- Modify: `radis/labels/utils/schemas.py`

- [ ] **Step 1: Replace the file contents**

```python
from enum import StrEnum
from typing import Any, Sequence

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


def build_label_classification_schema(labels: Sequence) -> type[BaseModel]:
    fields: dict[str, Any] = {
        lbl.name: (BucketValue, Field(description=lbl.description))
        for lbl in labels
    }
    return create_model("LabelClassification", **fields)


def build_gate_schema(groups: Sequence) -> type[BaseModel]:
    fields: dict[str, Any] = {
        g.name: (GateValue, Field(description=g.gate_question))
        for g in groups
    }
    return create_model("GateScreening", **fields)
```

- [ ] **Step 2: Verify the file looks right**

```bash
cat radis/labels/utils/schemas.py
```

---

## Task 2: Update `tests/unit/test_schemas.py`

**Files:**
- Modify: `radis/labels/tests/unit/test_schemas.py`

- [ ] **Step 1: Replace the file contents**

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
)


def _label(id, name="pneumonia", description="infection of the lung"):
    return SimpleNamespace(id=id, name=name, description=description)


def _group(id, name="pulmonary", gate_question="Is this a chest study?"):
    return SimpleNamespace(id=id, name=name, gate_question=gate_question)


def test_label_schema_fields_are_name_keyed_and_carry_description():
    Schema = build_label_classification_schema([_label(42, "pneumonia", "lung infection")])
    assert "pneumonia" in Schema.model_fields
    assert Schema.model_fields["pneumonia"].description == "lung infection"


def test_label_schema_accepts_all_five_buckets_and_rejects_unknown():
    Schema = build_label_classification_schema([_label(1)])
    for bucket in ("PRESENT", "LIKELY", "POSSIBLE", "ABSENT", "UNMENTIONED"):
        assert Schema.model_validate({"pneumonia": bucket}).model_dump()["pneumonia"] == bucket
    with pytest.raises(ValidationError):
        Schema.model_validate({"pneumonia": "BOGUS"})


def test_label_schema_field_is_required():
    Schema = build_label_classification_schema([_label(1)])
    with pytest.raises(ValidationError):
        Schema.model_validate({})


def test_gate_schema_fields_are_name_keyed_and_carry_question():
    Schema = build_gate_schema([_group(7, "pulmonary", "Does this report mention the lungs?")])
    assert "pulmonary" in Schema.model_fields
    assert Schema.model_fields["pulmonary"].description == "Does this report mention the lungs?"


def test_gate_schema_validates_yes_no_and_rejects_unknown():
    Schema = build_gate_schema([_group(1)])
    for value in ("YES", "NO"):
        assert Schema.model_validate({"pulmonary": value}).model_dump()["pulmonary"] == value
    with pytest.raises(ValidationError):
        Schema.model_validate({"pulmonary": "MAYBE"})
    with pytest.raises(ValidationError):
        Schema.model_validate({"pulmonary": "PROBABLY"})


def test_bucket_and_gate_enums_match_model_choices():
    """Drift guard: static enums must stay in sync with the model TextChoices."""
    assert {b.value for b in BucketValue} == {c.value for c in LabelResult.Value}
    assert {g.value for g in GateValue} == {c.value for c in GateAnswer.Value}
```

- [ ] **Step 2: Run unit schema tests — expect failures until models/labeling are updated**

```bash
uv run cli test -- radis/labels/tests/unit/test_schemas.py -v
```

Expected failures: `test_bucket_and_gate_enums_match_model_choices` (GateAnswer.Value still has MAYBE), rest should pass.

---

## Task 3: Update `models.py` — remove MAYBE

**Files:**
- Modify: `radis/labels/models.py`

- [ ] **Step 1: Remove MAYBE from `GateAnswer.Value`, tighten `max_length`, update comment**

In `models.py`, find the `GateAnswer` model and apply these three changes:

Change the `gate_question` field comment on `LabelGroup` (line ~14):
```python
gate_question = models.TextField()  # upfront Yes/No screening question for this group
```

Change `GateAnswer.Value` (remove MAYBE line):
```python
class Value(models.TextChoices):
    YES = "YES", "Yes"
    NO = "NO", "No"
```

Change `max_length` on the `value` field:
```python
value = models.CharField(max_length=3, choices=Value.choices)
```

- [ ] **Step 2: Run drift-guard test — should now pass**

```bash
uv run cli test -- radis/labels/tests/unit/test_schemas.py::test_bucket_and_gate_enums_match_model_choices -v
```

Expected: PASS

---

## Task 4: Update `scope.py` — remove MAYBE from condition B

**Files:**
- Modify: `radis/labels/scope.py`

- [ ] **Step 1: Replace `value__in` with `value=`**

Find this block in `_needs_work_queryset`:
```python
            GateAnswer.objects.filter(
                report=OuterRef("pk"),
                value__in=[GateAnswer.Value.YES, GateAnswer.Value.MAYBE],
                generated_at__gte=F("label_group__updated_at"),
            )
```

Replace with:
```python
            GateAnswer.objects.filter(
                report=OuterRef("pk"),
                value=GateAnswer.Value.YES,
                generated_at__gte=F("label_group__updated_at"),
            )
```

Also update the docstring on line 8:
```python
def _needs_work_queryset(active_group_count: int) -> QuerySet:
    """Reports needing labeling work: missing/stale gate (condition A) OR a fresh YES
    group with a missing/stale label result (condition B)."""
```

---

## Task 5: Update `tests/helpers.py` — name-keyed `FakeChatClient`

**Files:**
- Modify: `radis/labels/tests/helpers.py`

- [ ] **Step 1: Replace the file contents**

The current implementation detects gate vs label schema by checking if field names start with `"group_"`. With name-keyed fields that's no longer possible — switch to checking `schema.__name__` ("GateScreening" vs "LabelClassification"). Keys in `gate_values` and `label_values` switch from `int` (id) to `str` (name).

```python
"""Test double for ChatClient used by label_report.

extract_data inspects the dynamically-built schema's __name__ to decide whether this is
a gate call ("GateScreening") or a label call ("LabelClassification"), then returns a
valid instance populated from the configured answer maps (keyed by group/label name).
Recorded calls let tests assert exact gate vs. label LLM call counts.
"""

from pydantic import BaseModel


class FakeChatClient:
    def __init__(
        self,
        gate_values: dict[str, str] | None = None,
        label_values: dict[str, str] | None = None,
    ) -> None:
        self.gate_values = gate_values or {}
        self.label_values = label_values or {}
        self.gate_calls: list[list[str]] = []
        self.label_calls: list[list[str]] = []

    def extract_data(self, prompt: str, schema: type[BaseModel]) -> BaseModel:
        field_names = list(schema.model_fields.keys())
        if schema.__name__ == "GateScreening":
            self.gate_calls.append(field_names)
            data = {name: self.gate_values[name] for name in field_names}
        else:
            self.label_calls.append(field_names)
            data = {name: self.label_values[name] for name in field_names}
        return schema.model_validate(data)
```

---

## Task 6: Update `labeling.py` — inline parse helpers, collapse MAYBE

**Files:**
- Modify: `radis/labels/labeling.py`

- [ ] **Step 1: Remove parse helper imports**

Find the import block at the top:
```python
from .utils.schemas import (
    build_gate_schema,
    build_label_classification_schema,
    parse_gate_results,
    parse_label_results,
)
```

Replace with:
```python
from .utils.schemas import (
    build_gate_schema,
    build_label_classification_schema,
)
```

- [ ] **Step 2: Inline gate parse**

Find:
```python
        parsed = client.extract_data(render_gate_prompt(report.body), schema)
        new_gate_results.update(parse_gate_results(parsed))
```

Replace with:
```python
        parsed = client.extract_data(render_gate_prompt(report.body), schema)
        result_map = parsed.model_dump()
        for g in gate_batch:
            new_gate_results[g.id] = str(result_map[g.name])
```

- [ ] **Step 3: Collapse MAYBE comparisons — three spots**

Replace (lines ~79-82):
```python
                if new_value == GateAnswer.Value.NO and old_value in (
                    GateAnswer.Value.YES,
                    GateAnswer.Value.MAYBE,
                ):
```
With:
```python
                if new_value == GateAnswer.Value.NO and old_value == GateAnswer.Value.YES:
```

Replace (line ~85):
```python
            if new_value in (GateAnswer.Value.YES, GateAnswer.Value.MAYBE):
```
With:
```python
            if new_value == GateAnswer.Value.YES:
```

Replace (line ~91):
```python
            if gate_value in (GateAnswer.Value.YES, GateAnswer.Value.MAYBE):
```
With:
```python
            if gate_value == GateAnswer.Value.YES:
```

- [ ] **Step 4: Inline label parse in `_run_label_set`**

Find:
```python
def _run_label_set(client: ChatClient, report: Report, labels: list[Label]) -> None:
    schema = build_label_classification_schema(labels)
    parsed = client.extract_data(render_label_prompt(report.body), schema)
    for label_id, bucket in parse_label_results(parsed).items():
        LabelResult.objects.update_or_create(
            report=report, label_id=label_id, defaults={"value": bucket}
        )
```

Replace with:
```python
def _run_label_set(client: ChatClient, report: Report, labels: list[Label]) -> None:
    schema = build_label_classification_schema(labels)
    parsed = client.extract_data(render_label_prompt(report.body), schema)
    result_map = parsed.model_dump()
    for lbl in labels:
        LabelResult.objects.update_or_create(
            report=report, label_id=lbl.id, defaults={"value": result_map[lbl.name]}
        )
```

---

## Task 7: Update `test_labeling.py` — name-keyed calls, remove MAYBE tests

**Files:**
- Modify: `radis/labels/tests/test_labeling.py`

- [ ] **Step 1: Delete `test_gate_maybe_runs_labels`** (lines 185–200 — MAYBE no longer a valid gate value)

- [ ] **Step 2: Delete `test_gate_flip_maybe_to_no_deletes_results_atomically`** (lines 203–221 — covered by the existing `test_gate_flip_yes_to_no_deletes_results_atomically`)

- [ ] **Step 3: Update all `FakeChatClient` calls — switch from id-keyed to name-keyed**

Every `gate_values={group.id: "..."}` → `gate_values={group.name: "..."}`.
Every `label_values={label.id: "..."}` → `label_values={label.name: "..."}`.

Full list of lines to update:

`test_gate_no_skips_group_entirely` (line ~52):
```python
client = FakeChatClient(gate_values={group.name: "NO"})
```

`test_gate_yes_runs_labels_and_stores_all_buckets` (lines ~71-78):
```python
client = FakeChatClient(
    gate_values={group.name: "YES"},
    label_values={
        l_present.name: "PRESENT",
        l_absent.name: "ABSENT",
        l_unmentioned.name: "UNMENTIONED",
    },
)
```

`test_gate_batching_two_calls_for_twenty_groups` (line ~96):
```python
client = FakeChatClient(gate_values={g.name: "NO" for g in groups})
```

`test_fresh_gate_yes_with_one_stale_label_runs_only_that_label` (line ~136):
```python
client = FakeChatClient(label_values={stale_label.name: "ABSENT"})
```

`test_gate_flip_yes_to_no_deletes_results_atomically` (line ~157):
```python
client = FakeChatClient(gate_values={group.name: "NO"})
```

`test_stale_gate_new_yes_old_no_runs_all_labels` (line ~176):
```python
client = FakeChatClient(gate_values={group.name: "YES"}, label_values={label.name: "PRESENT"})
```

`test_stale_gate_yes_yes_with_fresh_results_makes_no_label_calls` (line ~237):
```python
client = FakeChatClient(gate_values={group.name: "YES"})
```

- [ ] **Step 4: Update `label_calls` assertions — switch from id to name**

`test_fresh_gate_yes_with_one_stale_label_runs_only_that_label` (line ~141):
```python
assert client.label_calls == [[stale_label.name]]
```

`test_stale_gate_new_yes_old_no_runs_all_labels` (line ~181):
```python
assert client.label_calls == [[label.name]]
```

- [ ] **Step 5: Run the labeling test suite**

```bash
uv run cli test -- radis/labels/tests/test_labeling.py -v
```

Expected: all remaining tests PASS

---

## Task 8: Update `test_scope.py` — remove MAYBE test

**Files:**
- Modify: `radis/labels/tests/test_scope.py`

- [ ] **Step 1: Delete `test_report_with_fresh_maybe_gate_but_missing_result_needs_work`**

This test (lines ~92–99) verifies MAYBE triggers condition B. That behavior is gone; delete the entire test function.

- [ ] **Step 2: Run scope tests**

```bash
uv run cli test -- radis/labels/tests/test_scope.py -v
```

Expected: all PASS

---

## Task 9: Update `test_prompts.py` — remove MAYBE from gate prompt assertion

**Files:**
- Modify: `radis/labels/tests/unit/test_prompts.py`

- [ ] **Step 1: Update `test_gate_prompt_substitutes_report_and_teaches_gate_values`**

Find:
```python
def test_gate_prompt_substitutes_report_and_teaches_gate_values():
    rendered = render_gate_prompt("report body here")
    assert "report body here" in rendered
    for value in ("YES", "NO", "MAYBE"):
        assert value in rendered
```

Replace with:
```python
def test_gate_prompt_substitutes_report_and_teaches_gate_values():
    rendered = render_gate_prompt("report body here")
    assert "report body here" in rendered
    for value in ("YES", "NO"):
        assert value in rendered
    assert "MAYBE" not in rendered
```

---

## Task 10: Update `settings/base.py` — remove MAYBE from default gate prompt

**Files:**
- Modify: `radis/settings/base.py`

- [ ] **Step 1: Update `_DEFAULT_GATE_SYSTEM_PROMPT`**

Find:
```python
# Generic gate (Yes/No/Maybe applicability) system prompt. Only $report is substituted.
_DEFAULT_GATE_SYSTEM_PROMPT = """
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
```

Replace with:
```python
# Generic gate (Yes/No applicability) system prompt. Only $report is substituted.
_DEFAULT_GATE_SYSTEM_PROMPT = """
You are an AI medical assistant. The provided schema lists one field per topic, each
field's description stating the topic's screening question. For every field, answer
whether the radiology report below contains content relevant to that topic, responding
with exactly one of:
  - "YES" — the report clearly contains relevant content
  - "NO"  — the report clearly does not

Return answers in JSON format matching the provided schema.

Radiology Report:
$report
"""
```

- [ ] **Step 2: Run prompt tests**

```bash
uv run cli test -- radis/labels/tests/unit/test_prompts.py -v
```

Expected: all PASS

---

## Task 11: Update `example.env`

**Files:**
- Modify: `example.env`

- [ ] **Step 1: Update the LABELING_GATE_SYSTEM_PROMPT comment**

Find:
```
# LABELING_GATE_SYSTEM_PROMPT=...   # generic group gate (Yes/No/Maybe) prompt
```

Replace with:
```
# LABELING_GATE_SYSTEM_PROMPT=...   # generic group gate (Yes/No) prompt
```

---

## Task 12: Squash migrations

**Files:**
- Delete + regenerate: `radis/labels/migrations/0001_initial.py`

- [ ] **Step 1: Delete the existing migration and regenerate**

```bash
rm radis/labels/migrations/0001_initial.py
uv run manage.py makemigrations labels --name initial
```

Expected output includes:
```
+ Create model GateAnswer  (with max_length=3, no MAYBE choice)
```

- [ ] **Step 2: Verify GateAnswer choices in new migration**

```bash
grep -A5 "gateanswer" radis/labels/migrations/0001_initial.py
```

Expected: `choices=[("YES", "Yes"), ("NO", "No")]` and `max_length=3`.

---

## Task 13: Run full labels test suite and commit

- [ ] **Step 1: Run all labels tests**

```bash
uv run cli test -- radis/labels/ -v
```

Expected: all PASS, no references to MAYBE in failures.

- [ ] **Step 2: Run linting**

```bash
uv run cli lint
```

Fix any issues before committing.

- [ ] **Step 3: Commit**

```bash
git add radis/labels/utils/schemas.py \
        radis/labels/labeling.py \
        radis/labels/models.py \
        radis/labels/scope.py \
        radis/labels/migrations/0001_initial.py \
        radis/labels/tests/helpers.py \
        radis/labels/tests/unit/test_schemas.py \
        radis/labels/tests/test_labeling.py \
        radis/labels/tests/test_scope.py \
        radis/labels/tests/unit/test_prompts.py \
        radis/settings/base.py \
        example.env
git commit -m "refactor(labels): name-keyed schema fields and gate YES/NO only"
```
