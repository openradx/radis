# Labels: name-keyed schema fields + gate YES/NO only

**Date:** 2026-06-08
**Branch:** feature/auto-labeling-design-kai-try1

## Context

Two related simplifications to the labels app's dynamic Pydantic schema layer:

1. Replace opaque ID-keyed field names (`label_42`, `group_7`) with the human-readable name
   (`lbl.name`, `g.name`) so the LLM sees self-documenting keys in the JSON schema.
2. Remove `MAYBE` from the gate enum — gate answers are strictly `YES` or `NO`.

Pydantic v2 and llama.cpp structured output both handle non-identifier string keys natively
(confirmed end-to-end in dev). No sanitization layer is needed.

## Change 1 — Name-keyed schema fields

### `radis/labels/utils/schemas.py`

`build_label_classification_schema`: key = `lbl.name`, `Field(description=lbl.description)`.
The label name moves from being a description prefix to the field key itself.

```python
def build_label_classification_schema(labels: Sequence) -> type[BaseModel]:
    fields: dict[str, Any] = {
        lbl.name: (BucketValue, Field(description=lbl.description))
        for lbl in labels
    }
    return create_model("LabelClassification", **fields)
```

`build_gate_schema`: key = `g.name`, `Field(description=g.gate_question)`.

```python
def build_gate_schema(groups: Sequence) -> type[BaseModel]:
    fields: dict[str, Any] = {
        g.name: (GateValue, Field(description=g.gate_question))
        for g in groups
    }
    return create_model("GateScreening", **fields)
```

`parse_label_results` and `parse_gate_results` are **deleted**. Callers inline the mapping.

### `radis/labels/labeling.py`

**Gate parse** — inline after each `extract_data` call:

```python
result_map = parsed.model_dump()          # {g.name: gate_value}
for g in gate_batch:
    new_gate_results[g.id] = str(result_map[g.name])
```

**Label parse** — inline in `_run_label_set`:

```python
result_map = parsed.model_dump()          # {lbl.name: bucket}
for lbl in labels:
    LabelResult.objects.update_or_create(
        report=report, label_id=lbl.id, defaults={"value": result_map[lbl.name]}
    )
```

## Change 2 — Gate YES/NO only

### `radis/labels/utils/schemas.py`

Remove `MAYBE` from `GateValue`:

```python
class GateValue(StrEnum):
    YES = "YES"
    NO = "NO"
```

### `radis/labels/models.py`

- `GateAnswer.Value`: remove `MAYBE` choice, tighten `max_length` 5 → 3.
- `LabelGroup.gate_question` field comment: `"upfront Yes/No/Maybe screening question"` →
  `"upfront Yes/No screening question"`.

### `radis/labels/labeling.py`

Three `in (YES, MAYBE)` tuple checks collapse to `== YES`:

| Line | Before | After |
|------|--------|-------|
| 79–82 | `old_value in (GateAnswer.Value.YES, GateAnswer.Value.MAYBE)` | `old_value == GateAnswer.Value.YES` |
| 85 | `new_value in (GateAnswer.Value.YES, GateAnswer.Value.MAYBE)` | `new_value == GateAnswer.Value.YES` |
| 91 | `gate_value in (GateAnswer.Value.YES, GateAnswer.Value.MAYBE)` | `gate_value == GateAnswer.Value.YES` |

### Migration

Single migration file, two operations in order:

1. **Data**: `GateAnswer.objects.filter(value="MAYBE").delete()` — existing MAYBE rows
   are deleted and will be regenerated on the next scan.
2. **Schema**: alter `GateAnswer.value` `max_length` 5 → 3.

## Tests (`tests/unit/test_schemas.py`)

**Delete:**
- `test_parse_label_results_round_trips_ids`
- `test_parse_gate_results_round_trips_ids`

**Update:**
- `test_label_schema_fields_are_id_keyed_and_carry_name_and_description` → assert `lbl.name`
  is the field key; description contains only `lbl.description`.
- `test_gate_schema_fields_are_id_keyed_and_carry_question` → assert `g.name` is the field key.
- `test_gate_schema_validates_yes_no_maybe_and_rejects_unknown` → remove MAYBE from valid
  values; rename to drop "maybe".
- `test_bucket_and_gate_enums_match_model_choices` → passes automatically once both enums
  are updated.

Grep for any other `GateAnswer.Value.MAYBE` or old parse helper references across the test
suite and update during implementation.

## Files touched

| File | Change |
|------|--------|
| `radis/labels/utils/schemas.py` | Name-keyed builders, remove parse helpers, remove `GateValue.MAYBE` |
| `radis/labels/labeling.py` | Inline parse logic, collapse MAYBE comparisons |
| `radis/labels/models.py` | Remove `GateAnswer.Value.MAYBE`, max_length, comment |
| `radis/labels/migrations/NNNN_*.py` | Data + schema migration |
| `radis/labels/tests/unit/test_schemas.py` | Delete/update affected tests |
