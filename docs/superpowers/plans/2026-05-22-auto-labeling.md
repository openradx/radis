# Auto-Labeling Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the auto-labeling feature defined in `docs/superpowers/specs/2026-05-21-auto-labeling-design.md` — a new `radis.labels` Django app that classifies radiology reports against admin-managed YES/NO/MAYBE questions, with chunked batch labeling on Report create/update and an admin-triggered singleton backfill for the existing corpus.

**Architecture:** New Django app `radis.labels` with denormalized `Question` (text + label + group string) and `Answer` (`report × question → YES|NO|MAYBE`) models. Both execution paths converge on a single per-report function `label_report` that includes a per-group idempotency check; both call the shared `label_reports_in_parallel` helper for concurrency.

**Tech Stack:** Python 3.12+, Django 5.1, PostgreSQL 17, Procrastinate, OpenAI-compatible LLM via `ChatClient`, Pydantic, pytest + pytest-django + factory-boy + Playwright (acceptance), Cotton components.

---

## File structure

New under `radis/labels/`:

- `__init__.py`, `apps.py`
- `models.py` — `Question`, `Answer`, `LabelingJob`, `LabelingTask`
- `factories.py`
- `prompts.py` — `sanitize_label`, `render_questions_prompt`, `build_yes_no_maybe_schema`
- `services.py` — `group_active_questions_by_group`, `upsert_answers`, `_group_answers_are_current`, `label_report`, `label_reports_in_parallel`, `find_reports_needing_work`, `create_labeling_tasks_streaming`
- `signals.py` — `_label_reports_handler`, `register_report_handlers`
- `tasks.py` — `label_report_batch`, `process_labeling_job`, `process_labeling_task`, `enqueue_all_pending_tasks`
- `processors.py` — `LabelingTaskProcessor`
- `admin.py` — `QuestionAdmin`, `AnswerAdmin`, `LabelingJobAdmin`, `AnswerInline`
- `admin_views.py` — `run_backfill_view`, `cancel_backfill_view`
- `migrations/0001_initial.py` and `migrations/000N_labelingjob_partial_unique.py`
- `templates/cotton/report_labels.html`
- `templates/labels/admin/labelingjob_changelist.html`
- `management/commands/labels_status.py`
- `tests/` — per-feature test modules

Existing files modified:

- `radis/settings/base.py` — settings + prompt constant + logger + INSTALLED_APPS
- `radis/reports/admin.py` — `AnswerInline` added to `ReportAdmin.inlines`
- `radis/reports/templates/reports/report_detail.html` — include `<c-report-labels />`
- `radis/reports/views.py` — prefetch on detail view
- `radis/pgsearch/providers.py` — labels filter block + `facet_label_counts`
- `radis/search/` (`models.py` / `forms.py` / `views.py` / templates) — multi-select for labels
- `cli.py` — register `labels-status`
- `example.env`, `CLAUDE.md`, `KNOWLEDGE.md`

---

# Phase 1 — Foundation

## Task 1: Scaffold the app and register it

**Files:**
- Create: `radis/labels/__init__.py`, `radis/labels/apps.py`, `radis/labels/signals.py`, `radis/labels/migrations/__init__.py`, `radis/labels/tests/__init__.py`, `radis/labels/tests/conftest.py`
- Modify: `radis/settings/base.py` (`INSTALLED_APPS`)

- [ ] **Step 1: Make directories and empty inits**

```bash
mkdir -p radis/labels/migrations radis/labels/tests radis/labels/management/commands radis/labels/templates/cotton radis/labels/templates/labels/admin
touch radis/labels/__init__.py radis/labels/migrations/__init__.py radis/labels/tests/__init__.py radis/labels/management/__init__.py radis/labels/management/commands/__init__.py
```

- [ ] **Step 2: Write `apps.py`**

Create `radis/labels/apps.py`:

```python
from django.apps import AppConfig


class LabelsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "radis.labels"

    def ready(self) -> None:
        from .signals import register_report_handlers

        register_report_handlers()
```

- [ ] **Step 3: Write empty signals stub**

Create `radis/labels/signals.py`:

```python
def register_report_handlers() -> None:
    """Filled in Task 15."""
    return None
```

- [ ] **Step 4: Add to INSTALLED_APPS**

In `radis/settings/base.py`, find `INSTALLED_APPS` (around line 53–94) and after `"radis.pgsearch.apps.PgSearchConfig",` add:

```python
    "radis.labels.apps.LabelsConfig",
```

- [ ] **Step 5: Verify the app loads**

```bash
uv run python manage.py check
```

Expected: "System check identified no issues" (or pre-existing warnings unchanged).

- [ ] **Step 6: conftest fixture for DB**

Create `radis/labels/tests/conftest.py`:

```python
import pytest


@pytest.fixture(autouse=True)
def _enable_db(db):
    return db
```

- [ ] **Step 7: Smoke-test that the test discovery finds the package**

```bash
uv run cli test -- radis/labels/ -q
```

Expected: "no tests ran" / 0 passed, no collection errors.

- [ ] **Step 8: Commit**

```bash
git add radis/labels/ radis/settings/base.py
git commit -m "feat(labels): scaffold radis.labels app and register in INSTALLED_APPS"
```

---

## Task 2: `Question` model

**Files:**
- Create: `radis/labels/models.py`
- Create: `radis/labels/factories.py`
- Create: `radis/labels/tests/test_models.py`

- [ ] **Step 1: Failing tests**

Create `radis/labels/tests/test_models.py`:

```python
import pytest
from django.db import IntegrityError

from radis.labels.factories import QuestionFactory
from radis.labels.models import Question


class TestQuestion:
    def test_str_returns_label(self):
        q = QuestionFactory(label="pneumonia")
        assert str(q) == "pneumonia"

    def test_default_active_is_true(self):
        assert QuestionFactory().active is True

    def test_label_is_unique(self):
        QuestionFactory(label="pneumonia")
        with pytest.raises(IntegrityError):
            QuestionFactory(label="pneumonia")

    def test_updated_at_advances_on_save(self):
        q = QuestionFactory()
        before = q.updated_at
        q.text = "edited"
        q.save()
        q.refresh_from_db()
        assert q.updated_at > before
```

- [ ] **Step 2: Factory stub**

Create `radis/labels/factories.py`:

```python
import factory
from factory.django import DjangoModelFactory

from .models import Question


class QuestionFactory(DjangoModelFactory):
    class Meta:
        model = Question

    text = factory.Faker("sentence")
    label = factory.Sequence(lambda n: f"label_{n}")
    group = "default"
    active = True
```

- [ ] **Step 3: Confirm tests fail**

```bash
uv run cli test -- radis/labels/tests/test_models.py -q
```

Expected: import error on `Question`.

- [ ] **Step 4: Implement model**

Create `radis/labels/models.py`:

```python
from django.db import models


class Question(models.Model):
    text = models.TextField()
    label = models.CharField(max_length=100)
    group = models.CharField(max_length=100)
    active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [models.Index(fields=["active", "group"])]
        constraints = [
            models.UniqueConstraint(fields=["label"], name="unique_question_label"),
        ]

    def __str__(self) -> str:
        return self.label
```

- [ ] **Step 5: Migrate and run tests**

```bash
uv run python manage.py makemigrations labels
uv run python manage.py migrate labels
uv run cli test -- radis/labels/tests/test_models.py -q
```

Expected: 4 tests pass.

- [ ] **Step 6: Commit**

```bash
git add radis/labels/models.py radis/labels/factories.py radis/labels/tests/test_models.py radis/labels/migrations/0001_initial.py
git commit -m "feat(labels): add Question model with unique label constraint"
```

---

## Task 3: `Answer` model

**Files:**
- Modify: `radis/labels/models.py`, `radis/labels/factories.py`, `radis/labels/tests/test_models.py`

- [ ] **Step 1: Failing tests**

Append to `radis/labels/tests/test_models.py`:

```python
from radis.reports.factories import ReportFactory
from radis.labels.factories import AnswerFactory
from radis.labels.models import Answer


class TestAnswer:
    def test_value_choices(self):
        assert set(Answer.Value.values) == {"YES", "NO", "MAYBE"}

    def test_unique_per_report_question(self):
        report = ReportFactory()
        q = QuestionFactory()
        AnswerFactory(report=report, question=q, value="YES")
        with pytest.raises(IntegrityError):
            AnswerFactory(report=report, question=q, value="NO")

    def test_generated_at_bumps_on_save(self):
        a = AnswerFactory(value="YES")
        before = a.generated_at
        a.value = "MAYBE"
        a.save()
        a.refresh_from_db()
        assert a.generated_at > before

    def test_cascade_with_question(self):
        a = AnswerFactory()
        q_id = a.question.id
        a.question.delete()
        assert not Answer.objects.filter(question_id=q_id).exists()

    def test_cascade_with_report(self):
        a = AnswerFactory()
        r_id = a.report.id
        a.report.delete()
        assert not Answer.objects.filter(report_id=r_id).exists()
```

- [ ] **Step 2: Factory for `Answer`**

Append to `radis/labels/factories.py`:

```python
from radis.reports.factories import ReportFactory
from .models import Answer


class AnswerFactory(DjangoModelFactory):
    class Meta:
        model = Answer

    report = factory.SubFactory(ReportFactory)
    question = factory.SubFactory(QuestionFactory)
    value = "YES"
```

- [ ] **Step 3: Run failing**

```bash
uv run cli test -- radis/labels/tests/test_models.py::TestAnswer -q
```

Expected: import error on `Answer`.

- [ ] **Step 4: Implement model**

Append to `radis/labels/models.py`:

```python
from radis.reports.models import Report


class Answer(models.Model):
    class Value(models.TextChoices):
        YES = "YES", "Yes"
        NO = "NO", "No"
        MAYBE = "MAYBE", "Maybe"

    report = models.ForeignKey(
        Report, on_delete=models.CASCADE, related_name="answers"
    )
    question = models.ForeignKey(
        Question, on_delete=models.CASCADE, related_name="answers"
    )
    value = models.CharField(max_length=5, choices=Value.choices)
    generated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["report", "question"], name="unique_answer_per_report_question"
            ),
        ]
        indexes = [
            models.Index(fields=["question", "value"]),
            models.Index(fields=["report"]),
        ]
```

- [ ] **Step 5: Migrate, run tests**

```bash
uv run python manage.py makemigrations labels
uv run python manage.py migrate labels
uv run cli test -- radis/labels/tests/test_models.py -q
```

Expected: all `TestQuestion` and `TestAnswer` pass.

- [ ] **Step 6: Commit**

```bash
git add radis/labels/models.py radis/labels/factories.py radis/labels/tests/test_models.py radis/labels/migrations/
git commit -m "feat(labels): add Answer model with unique (report, question) constraint"
```

---

## Task 4: `LabelingJob` + `LabelingTask` models

**Files:**
- Modify: `radis/labels/models.py`, `radis/labels/factories.py`, `radis/labels/tests/test_models.py`
- Modify: `radis/settings/base.py` (`LABELING_BACKFILL_PRIORITY` minimal)

- [ ] **Step 1: Add the minimal priority setting needed for import**

In `radis/settings/base.py`, after the existing `SUBSCRIPTION_…` block:

```python
LABELING_BACKFILL_PRIORITY = env.int("LABELING_BACKFILL_PRIORITY", default=0)
```

(Full settings block lands in Task 6.)

- [ ] **Step 2: Failing tests**

Append to `radis/labels/tests/test_models.py`:

```python
from radis.core.models import AnalysisJob, AnalysisTask
from radis.labels.factories import LabelingJobFactory, LabelingTaskFactory
from radis.labels.models import LabelingJob, LabelingTask


class TestLabelingJobModel:
    def test_inherits_from_analysis_job(self):
        assert issubclass(LabelingJob, AnalysisJob)

    def test_active_statuses_constant(self):
        assert AnalysisJob.Status.PREPARING in LabelingJob.ACTIVE_STATUSES
        assert AnalysisJob.Status.IN_PROGRESS in LabelingJob.ACTIVE_STATUSES
        assert AnalysisJob.Status.SUCCESS not in LabelingJob.ACTIVE_STATUSES


class TestLabelingTaskModel:
    def test_inherits_from_analysis_task(self):
        assert issubclass(LabelingTask, AnalysisTask)

    def test_reports_m2m(self):
        from radis.reports.factories import ReportFactory
        r = ReportFactory()
        t = LabelingTaskFactory()
        t.reports.add(r)
        assert r in t.reports.all()
```

- [ ] **Step 3: Factories**

Before adding factories, confirm the user factory path. Grep:

```bash
grep -rn "UserFactory" radis/extractions/ radis/subscriptions/ --include="*.py" | head -5
```

Use whichever import path is in use (commonly `adit_radis_shared.accounts.factories.UserFactory`). Append to `radis/labels/factories.py`:

```python
from radis.core.models import AnalysisJob
from adit_radis_shared.accounts.factories import UserFactory  # adjust to verified path
from .models import LabelingJob, LabelingTask


class LabelingJobFactory(DjangoModelFactory):
    class Meta:
        model = LabelingJob

    owner = factory.SubFactory(UserFactory)
    status = AnalysisJob.Status.UNVERIFIED


class LabelingTaskFactory(DjangoModelFactory):
    class Meta:
        model = LabelingTask

    job = factory.SubFactory(LabelingJobFactory)
```

- [ ] **Step 4: Implement models**

Append to `radis/labels/models.py`:

```python
from django.conf import settings
from radis.core.models import AnalysisJob, AnalysisTask


class LabelingJob(AnalysisJob):
    """Singleton backfill job. At most one row may be in an active status."""

    ACTIVE_STATUSES = (
        AnalysisJob.Status.UNVERIFIED,
        AnalysisJob.Status.PREPARING,
        AnalysisJob.Status.PENDING,
        AnalysisJob.Status.IN_PROGRESS,
        AnalysisJob.Status.CANCELING,
    )

    default_priority = settings.LABELING_BACKFILL_PRIORITY
    urgent_priority = settings.LABELING_BACKFILL_PRIORITY

    def delay(self) -> None:
        # Filled in Task 20.
        raise NotImplementedError("Implemented in Task 20")


class LabelingTask(AnalysisTask):
    job = models.ForeignKey(
        LabelingJob, on_delete=models.CASCADE, related_name="tasks"
    )
    reports = models.ManyToManyField(Report, related_name="+")
```

- [ ] **Step 5: Migrate and run tests**

```bash
uv run python manage.py makemigrations labels
uv run python manage.py migrate labels
uv run cli test -- radis/labels/tests/test_models.py -q
```

Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add radis/labels/models.py radis/labels/factories.py radis/labels/tests/test_models.py radis/labels/migrations/ radis/settings/base.py
git commit -m "feat(labels): add LabelingJob and LabelingTask models"
```

---

## Task 5: Partial unique index for backfill singleton

**Files:**
- Create: `radis/labels/migrations/000N_labelingjob_partial_unique.py`
- Create: `radis/labels/tests/test_singleton.py`

- [ ] **Step 1: Failing tests**

Create `radis/labels/tests/test_singleton.py`:

```python
import pytest
from django.db import IntegrityError, transaction

from radis.core.models import AnalysisJob
from radis.labels.factories import LabelingJobFactory


class TestLabelingJobSingleton:
    @pytest.mark.parametrize(
        "first_status",
        [
            AnalysisJob.Status.UNVERIFIED,
            AnalysisJob.Status.PREPARING,
            AnalysisJob.Status.PENDING,
            AnalysisJob.Status.IN_PROGRESS,
            AnalysisJob.Status.CANCELING,
        ],
    )
    def test_blocks_second_active_job(self, first_status):
        LabelingJobFactory(status=first_status)
        with pytest.raises(IntegrityError):
            with transaction.atomic():
                LabelingJobFactory(status=AnalysisJob.Status.PENDING)

    @pytest.mark.parametrize(
        "terminal_status",
        [
            AnalysisJob.Status.SUCCESS,
            AnalysisJob.Status.WARNING,
            AnalysisJob.Status.FAILURE,
            AnalysisJob.Status.CANCELED,
        ],
    )
    def test_allows_new_after_terminal(self, terminal_status):
        first = LabelingJobFactory(status=AnalysisJob.Status.PENDING)
        first.status = terminal_status
        first.save()
        LabelingJobFactory(status=AnalysisJob.Status.PENDING)
```

- [ ] **Step 2: Confirm tests fail**

```bash
uv run cli test -- radis/labels/tests/test_singleton.py -q
```

Expected: tests fail (no constraint exists yet).

- [ ] **Step 3: Find the actual stored values of `AnalysisJob.Status`**

```bash
uv run cli shell -c "from radis.core.models import AnalysisJob; print({s.name: s.value for s in AnalysisJob.Status})"
```

Record the values for `UNVERIFIED`, `PREPARING`, `PENDING`, `IN_PROGRESS`, `CANCELING`.

- [ ] **Step 4: Create an empty migration and add `RunSQL`**

```bash
uv run python manage.py makemigrations labels --empty --name labelingjob_partial_unique
```

Fill the new migration with:

```python
from django.db import migrations


CREATE_INDEX_SQL = """
CREATE UNIQUE INDEX labels_labelingjob_one_active_idx
ON labels_labelingjob ((1))
WHERE status IN ({active_values});
"""

DROP_INDEX_SQL = "DROP INDEX IF EXISTS labels_labelingjob_one_active_idx;"


class Migration(migrations.Migration):
    dependencies = [
        ("labels", "<previous migration filename without .py>"),
    ]

    operations = [
        migrations.RunSQL(
            sql=CREATE_INDEX_SQL.format(
                # Quoted comma-separated list from Step 3, e.g. "'UV','PR','PE','IP','CI'"
                active_values="<paste from Step 3>"
            ),
            reverse_sql=DROP_INDEX_SQL,
        ),
    ]
```

Replace placeholders with the values from Step 3.

- [ ] **Step 5: Apply migration**

```bash
uv run python manage.py migrate labels
uv run cli test -- radis/labels/tests/test_singleton.py -q
```

Expected: all tests pass.

- [ ] **Step 6: Commit**

```bash
git add radis/labels/migrations/ radis/labels/tests/test_singleton.py
git commit -m "feat(labels): enforce singleton active backfill via partial unique index"
```

---

# Phase 2 — Core labeling services

## Task 6: Settings + default prompt

**Files:**
- Modify: `radis/settings/base.py`
- Modify: `example.env`

- [ ] **Step 1: Add prompt constant and settings**

In `radis/settings/base.py`, near the existing `QUESTIONS_SYSTEM_PROMPT` / `OUTPUT_FIELDS_SYSTEM_PROMPT` constants, add:

```python
DEFAULT_LABELING_SYSTEM_PROMPT = """
You are an AI medical assistant analyzing a radiology report. Answer each of the questions
below independently, based only on what is stated or strongly implied in the report.

For each question, respond with exactly one of:
  - "YES"   — the answer is yes / the finding is present
  - "NO"    — the answer is no / the finding is absent
  - "MAYBE" — the report is genuinely ambiguous; do not use this when the answer is clear

Return the answers in JSON format matching the provided schema.

Radiology Report:
$report

Questions:
$questions
"""
```

Replace the `LABELING_BACKFILL_PRIORITY` line from Task 4 with the full block:

```python
# Labeling feature
LABELING_INGEST_PRIORITY   = env.int("LABELING_INGEST_PRIORITY",   default=1)
LABELING_BACKFILL_PRIORITY = env.int("LABELING_BACKFILL_PRIORITY", default=0)
LABELING_TASK_BATCH_SIZE       = env.int("LABELING_TASK_BATCH_SIZE",       default=100)
LABELING_LLM_CONCURRENCY_LIMIT = env.int("LABELING_LLM_CONCURRENCY_LIMIT", default=6)
LABELING_SYSTEM_PROMPT = env.str(
    "LABELING_SYSTEM_PROMPT", default=DEFAULT_LABELING_SYSTEM_PROMPT
)
```

- [ ] **Step 2: Update example.env**

Append:

```bash
# Labeling feature
LABELING_INGEST_PRIORITY=1
LABELING_BACKFILL_PRIORITY=0
LABELING_TASK_BATCH_SIZE=100
LABELING_LLM_CONCURRENCY_LIMIT=6
# LABELING_SYSTEM_PROMPT can be left unset to use the in-code default.
```

- [ ] **Step 3: Verify**

```bash
uv run cli shell -c "from django.conf import settings; print(settings.LABELING_INGEST_PRIORITY, settings.LABELING_TASK_BATCH_SIZE, len(settings.LABELING_SYSTEM_PROMPT) > 0)"
```

Expected: `1 100 True`.

- [ ] **Step 4: Commit**

```bash
git add radis/settings/base.py example.env
git commit -m "feat(labels): labeling settings and default system prompt"
```

---

## Task 7: `render_questions_prompt`

**Files:**
- Create: `radis/labels/prompts.py`
- Create: `radis/labels/tests/test_prompts.py`

- [ ] **Step 1: Failing tests**

Create `radis/labels/tests/test_prompts.py`:

```python
from radis.labels.factories import QuestionFactory
from radis.labels.prompts import render_questions_prompt


def test_substitutes_report_and_questions():
    q = QuestionFactory(text="Is the chest clear?", label="chest_clear")
    out = render_questions_prompt("body text", [q])
    assert "body text" in out
    assert "Is the chest clear?" in out
    assert "chest_clear" in out
    assert "$report" not in out
    assert "$questions" not in out


def test_handles_unicode():
    q = QuestionFactory(text="Frage über Lungen?", label="lung_de")
    out = render_questions_prompt("Bericht: keine Auffälligkeiten.", [q])
    assert "Frage über Lungen?" in out
    assert "keine Auffälligkeiten." in out
```

- [ ] **Step 2: Implement**

Create `radis/labels/prompts.py`:

```python
from string import Template

from django.conf import settings

from .models import Question


def _format_question_lines(questions: list[Question]) -> str:
    return "\n".join(f"- {q.label}: {q.text}" for q in questions)


def render_questions_prompt(report_body: str, questions: list[Question]) -> str:
    return Template(settings.LABELING_SYSTEM_PROMPT).substitute(
        report=report_body,
        questions=_format_question_lines(questions),
    )
```

- [ ] **Step 3: Run tests, commit**

```bash
uv run cli test -- radis/labels/tests/test_prompts.py -q
git add radis/labels/prompts.py radis/labels/tests/test_prompts.py
git commit -m "feat(labels): render the questions prompt"
```

---

## Task 8: `sanitize_label` and `build_yes_no_maybe_schema`

**Files:**
- Modify: `radis/labels/prompts.py`
- Create: `radis/labels/tests/test_schemas.py`

- [ ] **Step 1: Failing tests**

Create `radis/labels/tests/test_schemas.py`:

```python
import pytest
from pydantic import ValidationError

from radis.labels.factories import QuestionFactory
from radis.labels.prompts import build_yes_no_maybe_schema, sanitize_label


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("pneumonia", "pneumonia"),
        ("Lung Effusion", "lung_effusion"),
        ("foo-bar", "foo_bar"),
        ("123abc", "_123abc"),
        ("a b c", "a_b_c"),
    ],
)
def test_sanitize_label(raw, expected):
    out = sanitize_label(raw)
    assert out == expected
    assert out.isidentifier()


class TestBuildSchema:
    def test_one_field_per_question(self):
        q1 = QuestionFactory(label="pneumonia")
        q2 = QuestionFactory(label="effusion")
        Schema = build_yes_no_maybe_schema([q1, q2])
        inst = Schema(pneumonia="YES", effusion="MAYBE")
        assert inst.pneumonia == "YES" and inst.effusion == "MAYBE"

    def test_rejects_unknown_value(self):
        Schema = build_yes_no_maybe_schema([QuestionFactory(label="x")])
        with pytest.raises(ValidationError):
            Schema(x="PROBABLY")

    def test_rejects_extra_field(self):
        Schema = build_yes_no_maybe_schema([QuestionFactory(label="x")])
        with pytest.raises(ValidationError):
            Schema(x="YES", extra="YES")

    def test_rejects_missing_field(self):
        Schema = build_yes_no_maybe_schema(
            [QuestionFactory(label="x"), QuestionFactory(label="y")]
        )
        with pytest.raises(ValidationError):
            Schema(x="YES")

    def test_label_collision_raises(self):
        q1 = QuestionFactory(label="lung effusion")
        q2 = QuestionFactory(label="lung-effusion")
        with pytest.raises(ValueError, match="collide"):
            build_yes_no_maybe_schema([q1, q2])
```

- [ ] **Step 2: Implement**

Append to `radis/labels/prompts.py`:

```python
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, create_model


def sanitize_label(label: str) -> str:
    s = re.sub(r"[^A-Za-z0-9]", "_", label).lower()
    if not s or s[0].isdigit():
        s = "_" + s
    return s


def build_yes_no_maybe_schema(questions: list[Question]) -> type[BaseModel]:
    fields: dict[str, tuple[type, object]] = {}
    seen: dict[str, str] = {}
    for q in questions:
        key = sanitize_label(q.label)
        if key in seen:
            raise ValueError(
                f"Sanitized labels collide: {seen[key]!r} and {q.label!r} → {key!r}"
            )
        seen[key] = q.label
        fields[key] = (Literal["YES", "NO", "MAYBE"], ...)
    return create_model(
        "LabelingAnswers",
        __config__=ConfigDict(extra="forbid"),
        **fields,
    )
```

- [ ] **Step 3: Run tests, commit**

```bash
uv run cli test -- radis/labels/tests/test_schemas.py -q
git add radis/labels/prompts.py radis/labels/tests/test_schemas.py
git commit -m "feat(labels): sanitize labels and build the YES/NO/MAYBE Pydantic schema"
```

---

## Task 9: `group_active_questions_by_group`

**Files:**
- Create: `radis/labels/services.py`
- Create: `radis/labels/tests/test_services.py`

- [ ] **Step 1: Failing tests**

Create `radis/labels/tests/test_services.py`:

```python
from radis.labels.factories import QuestionFactory
from radis.labels.services import group_active_questions_by_group


def test_empty():
    assert group_active_questions_by_group() == {}


def test_groups_by_group_string():
    QuestionFactory(label="a", group="lung", active=True)
    QuestionFactory(label="b", group="lung", active=True)
    QuestionFactory(label="c", group="cardiac", active=True)
    out = group_active_questions_by_group()
    assert {q.label for q in out["lung"]} == {"a", "b"}
    assert {q.label for q in out["cardiac"]} == {"c"}


def test_excludes_inactive():
    QuestionFactory(label="on", group="lung", active=True)
    QuestionFactory(label="off", group="lung", active=False)
    out = group_active_questions_by_group()
    assert [q.label for q in out["lung"]] == ["on"]
```

- [ ] **Step 2: Implement**

Create `radis/labels/services.py`:

```python
from collections import defaultdict

from .models import Question


def group_active_questions_by_group() -> dict[str, list[Question]]:
    grouped: dict[str, list[Question]] = defaultdict(list)
    for q in Question.objects.filter(active=True).order_by("group", "label"):
        grouped[q.group].append(q)
    return dict(grouped)
```

- [ ] **Step 3: Test, commit**

```bash
uv run cli test -- radis/labels/tests/test_services.py -q
git add radis/labels/services.py radis/labels/tests/test_services.py
git commit -m "feat(labels): group active questions by group string"
```

---

## Task 10: `upsert_answers`

**Files:**
- Modify: `radis/labels/services.py`, `radis/labels/tests/test_services.py`

- [ ] **Step 1: Failing tests**

Append to `radis/labels/tests/test_services.py`:

```python
import time

from radis.reports.factories import ReportFactory
from radis.labels.models import Answer
from radis.labels.prompts import sanitize_label
from radis.labels.services import upsert_answers


def test_upsert_creates_rows():
    r = ReportFactory()
    q1 = QuestionFactory(label="pneumonia")
    q2 = QuestionFactory(label="effusion")
    upsert_answers(
        r, [q1, q2], {sanitize_label("pneumonia"): "YES", sanitize_label("effusion"): "MAYBE"}
    )
    assert Answer.objects.get(report=r, question=q1).value == "YES"
    assert Answer.objects.get(report=r, question=q2).value == "MAYBE"


def test_upsert_replaces_existing():
    r = ReportFactory()
    q = QuestionFactory(label="x")
    upsert_answers(r, [q], {sanitize_label("x"): "YES"})
    first = Answer.objects.get(report=r, question=q)
    time.sleep(0.01)
    upsert_answers(r, [q], {sanitize_label("x"): "NO"})
    second = Answer.objects.get(report=r, question=q)
    assert second.value == "NO"
    assert second.generated_at > first.generated_at


def test_upsert_ignores_unknown_keys():
    r = ReportFactory()
    q = QuestionFactory(label="x")
    upsert_answers(r, [q], {sanitize_label("x"): "YES", "garbage_key": "YES"})
    assert Answer.objects.filter(report=r).count() == 1
```

- [ ] **Step 2: Implement**

Append to `radis/labels/services.py`:

```python
from typing import Mapping

from radis.reports.models import Report
from .models import Answer
from .prompts import sanitize_label


def upsert_answers(
    report: Report,
    questions: list[Question],
    parsed: Mapping[str, str],
) -> None:
    for q in questions:
        key = sanitize_label(q.label)
        if key not in parsed:
            continue
        Answer.objects.update_or_create(
            report=report,
            question=q,
            defaults={"value": parsed[key]},
        )
```

- [ ] **Step 3: Test, commit**

```bash
uv run cli test -- radis/labels/tests/test_services.py -q
git add radis/labels/services.py radis/labels/tests/test_services.py
git commit -m "feat(labels): upsert one answer per (report, question)"
```

---

## Task 11: `_group_answers_are_current` (per-group idempotency)

**Files:**
- Modify: `radis/labels/services.py`
- Create: `radis/labels/tests/test_idempotency.py`

- [ ] **Step 1: Failing tests**

Create `radis/labels/tests/test_idempotency.py`:

```python
from datetime import timedelta

from django.utils import timezone

from radis.reports.factories import ReportFactory
from radis.labels.factories import AnswerFactory, QuestionFactory
from radis.labels.models import Answer, Question
from radis.labels.services import _group_answers_are_current


def _existing(report):
    return {
        a.question_id: a
        for a in Answer.objects.filter(report=report).select_related("question")
    }


class TestGroupAnswersAreCurrent:
    def test_all_current(self):
        r = ReportFactory()
        q = QuestionFactory()
        a = AnswerFactory(report=r, question=q)
        Question.objects.filter(pk=q.pk).update(updated_at=a.generated_at - timedelta(seconds=1))
        r.refresh_from_db()
        q.refresh_from_db()
        assert _group_answers_are_current([q], _existing(r), r.updated_at) is True

    def test_missing_answer_means_not_current(self):
        r = ReportFactory()
        q = QuestionFactory()
        assert _group_answers_are_current([q], _existing(r), r.updated_at) is False

    def test_question_edited_after_answer(self):
        r = ReportFactory()
        q = QuestionFactory()
        a = AnswerFactory(report=r, question=q)
        future = a.generated_at + timedelta(seconds=10)
        Question.objects.filter(pk=q.pk).update(updated_at=future)
        q.refresh_from_db()
        assert _group_answers_are_current([q], _existing(r), r.updated_at) is False

    def test_report_updated_after_answer(self):
        r = ReportFactory()
        q = QuestionFactory()
        a = AnswerFactory(report=r, question=q)
        future = a.generated_at + timedelta(seconds=10)
        type(r).objects.filter(pk=r.pk).update(updated_at=future)
        r.refresh_from_db()
        assert _group_answers_are_current([q], _existing(r), r.updated_at) is False
```

- [ ] **Step 2: Implement**

Append to `radis/labels/services.py`:

```python
from datetime import datetime


def _group_answers_are_current(
    questions: list[Question],
    existing: dict[int, Answer],
    report_updated_at: datetime,
) -> bool:
    for q in questions:
        a = existing.get(q.id)
        if a is None:
            return False
        if a.generated_at < q.updated_at:
            return False
        if a.generated_at < report_updated_at:
            return False
    return True
```

- [ ] **Step 3: Test, commit**

```bash
uv run cli test -- radis/labels/tests/test_idempotency.py -q
git add radis/labels/services.py radis/labels/tests/test_idempotency.py
git commit -m "feat(labels): per-group answers-are-current check"
```

---

## Task 12: `label_report` (per-report core)

**Files:**
- Modify: `radis/labels/services.py`, `radis/labels/tests/test_services.py`

- [ ] **Step 1: Failing tests**

Append to `radis/labels/tests/test_services.py`:

```python
from unittest.mock import MagicMock, patch

from radis.labels.services import label_report


class TestLabelReport:
    def test_skips_empty_body(self):
        r = ReportFactory(body="   ")
        QuestionFactory(label="x", group="g", active=True)
        with patch("radis.labels.services.ChatClient") as ChatClientMock:
            label_report(r.id)
        ChatClientMock.assert_not_called()
        assert Answer.objects.count() == 0

    def test_skips_no_active_questions(self):
        r = ReportFactory(body="some body")
        with patch("radis.labels.services.ChatClient") as ChatClientMock:
            label_report(r.id)
        ChatClientMock.assert_not_called()

    def test_one_llm_call_per_group(self):
        r = ReportFactory(body="some body")
        QuestionFactory(label="a", group="g1", active=True)
        QuestionFactory(label="b", group="g1", active=True)
        QuestionFactory(label="c", group="g2", active=True)
        with patch("radis.labels.services.ChatClient") as ChatClientMock:
            ChatClientMock.return_value.extract_data.side_effect = (
                lambda prompt, Schema: Schema(**{f: "YES" for f in Schema.model_fields})
            )
            label_report(r.id)
            assert ChatClientMock.return_value.extract_data.call_count == 2

    def test_persists_answers(self):
        r = ReportFactory(body="b")
        q = QuestionFactory(label="x", group="g", active=True)
        with patch("radis.labels.services.ChatClient") as ChatClientMock:
            ChatClientMock.return_value.extract_data.side_effect = (
                lambda prompt, Schema: Schema(x="MAYBE")
            )
            label_report(r.id)
        assert Answer.objects.get(report=r, question=q).value == "MAYBE"

    def test_skips_currently_labeled_group(self):
        """Per-group idempotency: when all answers in a group are current, no LLM call fires for that group."""
        r = ReportFactory(body="b")
        q = QuestionFactory(label="x", group="g", active=True)
        # Pre-populate a current answer (generated_at >= question.updated_at and >= report.updated_at).
        AnswerFactory(report=r, question=q, value="YES")
        # Touch report.updated_at to be slightly before the answer.
        Report.objects.filter(pk=r.pk).update(
            updated_at=Answer.objects.get(report=r).generated_at - timedelta(seconds=1)
        )
        with patch("radis.labels.services.ChatClient") as ChatClientMock:
            label_report(r.id)
        ChatClientMock.assert_not_called()
```

(Add `from datetime import timedelta` and `from radis.reports.models import Report` to the imports as needed.)

- [ ] **Step 2: Implement**

Append to `radis/labels/services.py`:

```python
import logging

from radis.chats.utils.chat_client import ChatClient
from .prompts import build_yes_no_maybe_schema, render_questions_prompt


logger = logging.getLogger("radis.labels")


def label_report(report_id: int, client: ChatClient | None = None) -> None:
    report = Report.objects.get(id=report_id)
    if not report.body or not report.body.strip():
        logger.info("labels.skip empty body: report %s", report_id)
        return

    questions_by_group = group_active_questions_by_group()
    if not questions_by_group:
        logger.info("labels.skip no active questions: report %s", report_id)
        return

    existing = {
        a.question_id: a
        for a in Answer.objects.filter(report=report).select_related("question")
    }
    chat = client or ChatClient()
    for group_str, questions in questions_by_group.items():
        if _group_answers_are_current(questions, existing, report.updated_at):
            continue
        Schema = build_yes_no_maybe_schema(questions)
        prompt = render_questions_prompt(report.body, questions)
        parsed = chat.extract_data(prompt, Schema)
        upsert_answers(report, questions, parsed.model_dump())
```

- [ ] **Step 3: Test, commit**

```bash
uv run cli test -- radis/labels/tests/test_services.py::TestLabelReport -q
git add radis/labels/services.py radis/labels/tests/test_services.py
git commit -m "feat(labels): label_report with per-group idempotency"
```

---

## Task 13: `label_reports_in_parallel` (shared helper)

**Files:**
- Modify: `radis/labels/services.py`
- Create: `radis/labels/tests/test_parallel.py`

- [ ] **Step 1: Failing tests**

Create `radis/labels/tests/test_parallel.py`:

```python
from unittest.mock import patch

from radis.reports.factories import ReportFactory
from radis.labels.factories import QuestionFactory
from radis.labels.models import Answer
from radis.labels.services import label_reports_in_parallel


def test_returns_success_and_failure_counts():
    r1 = ReportFactory(body="ok 1")
    r2 = ReportFactory(body="ok 2")
    r3 = ReportFactory(body="boom")
    QuestionFactory(label="x", group="g", active=True)

    def fake_extract(prompt, Schema):
        if "boom" in prompt:
            raise RuntimeError("LLM down")
        return Schema(**{f: "YES" for f in Schema.model_fields})

    with patch("radis.labels.services.ChatClient") as ChatClientMock:
        ChatClientMock.return_value.extract_data.side_effect = fake_extract
        ok, fail = label_reports_in_parallel([r1.id, r2.id, r3.id])
    assert ok == 2 and fail == 1
    assert Answer.objects.filter(report=r1).exists()
    assert Answer.objects.filter(report=r2).exists()
    assert not Answer.objects.filter(report=r3).exists()
```

- [ ] **Step 2: Implement**

Append to `radis/labels/services.py`:

```python
from concurrent.futures import ThreadPoolExecutor

from django.conf import settings


def label_reports_in_parallel(
    report_ids: list[int], client: ChatClient | None = None
) -> tuple[int, int]:
    chat = client or ChatClient()
    success = failure = 0
    with ThreadPoolExecutor(
        max_workers=settings.LABELING_LLM_CONCURRENCY_LIMIT
    ) as executor:
        futures = [executor.submit(label_report, rid, chat) for rid in report_ids]
        for f in futures:
            try:
                f.result()
                success += 1
            except Exception as exc:  # noqa: BLE001 — log and continue.
                logger.exception("labels.report.failed: %s", exc)
                failure += 1
    return success, failure
```

- [ ] **Step 3: Test, commit**

```bash
uv run cli test -- radis/labels/tests/test_parallel.py -q
git add radis/labels/services.py radis/labels/tests/test_parallel.py
git commit -m "feat(labels): label_reports_in_parallel shared helper"
```

---

# Phase 3 — Ingest path

## Task 14: `label_report_batch` Procrastinate task

**Files:**
- Create: `radis/labels/tasks.py`
- Create: `radis/labels/tests/test_tasks.py`

- [ ] **Step 1: Failing tests**

Create `radis/labels/tests/test_tasks.py`:

```python
from unittest.mock import patch

from radis.labels.tasks import label_report_batch


def test_label_report_batch_calls_parallel_helper():
    with patch("radis.labels.tasks.label_reports_in_parallel") as helper:
        label_report_batch(report_ids=[1, 2, 3])
    helper.assert_called_once_with([1, 2, 3])
```

- [ ] **Step 2: Implement**

Create `radis/labels/tasks.py`:

```python
from procrastinate.contrib.django import app

from .services import label_reports_in_parallel


@app.task(queue="llm")
def label_report_batch(report_ids: list[int]) -> None:
    label_reports_in_parallel(report_ids)
```

- [ ] **Step 3: Test, commit**

```bash
uv run cli test -- radis/labels/tests/test_tasks.py -q
git add radis/labels/tasks.py radis/labels/tests/test_tasks.py
git commit -m "feat(labels): label_report_batch task on llm queue"
```

---

## Task 15: Handler registration with chunking

**Files:**
- Modify: `radis/labels/signals.py`
- Create: `radis/labels/tests/test_signals.py`

- [ ] **Step 1: Failing tests**

Create `radis/labels/tests/test_signals.py`:

```python
from unittest.mock import patch

import pytest

from radis.reports.factories import ReportFactory
from radis.reports.site import (
    reports_created_handlers,
    reports_updated_handlers,
)
from radis.labels.signals import (
    HANDLER_NAME,
    _label_reports_handler,
    register_report_handlers,
)


class TestRegistration:
    def test_registers_created_and_updated(self):
        register_report_handlers()
        assert any(h.name == HANDLER_NAME for h in reports_created_handlers)
        assert any(h.name == HANDLER_NAME for h in reports_updated_handlers)

    def test_idempotent(self):
        register_report_handlers()
        before_c = sum(1 for h in reports_created_handlers if h.name == HANDLER_NAME)
        before_u = sum(1 for h in reports_updated_handlers if h.name == HANDLER_NAME)
        register_report_handlers()
        assert sum(1 for h in reports_created_handlers if h.name == HANDLER_NAME) == before_c
        assert sum(1 for h in reports_updated_handlers if h.name == HANDLER_NAME) == before_u


class TestHandlerChunking:
    @pytest.mark.parametrize(
        "n, expected_chunks",
        [(1, 1), (100, 1), (101, 2), (250, 3), (0, 0)],
    )
    def test_chunks_correctly(self, n, expected_chunks, settings):
        settings.LABELING_TASK_BATCH_SIZE = 100
        reports = [ReportFactory() for _ in range(n)]
        with patch("radis.labels.signals.app") as app_mock:
            deferrer = app_mock.configure_task.return_value
            _label_reports_handler(reports)
        assert deferrer.defer.call_count == expected_chunks

    def test_uses_ingest_priority(self, settings):
        settings.LABELING_INGEST_PRIORITY = 7
        with patch("radis.labels.signals.app") as app_mock:
            _label_reports_handler([ReportFactory()])
        _, kw = app_mock.configure_task.call_args
        assert kw["priority"] == 7

    def test_preserves_report_ids(self):
        reports = [ReportFactory() for _ in range(5)]
        ids = [r.id for r in reports]
        with patch("radis.labels.signals.app") as app_mock:
            deferrer = app_mock.configure_task.return_value
            _label_reports_handler(reports)
        deferred_ids = [c.kwargs["report_ids"] for c in deferrer.defer.call_args_list]
        assert sorted(sum(deferred_ids, [])) == sorted(ids)
```

- [ ] **Step 2: Implement**

Replace `radis/labels/signals.py`:

```python
import logging

from django.conf import settings
from procrastinate.contrib.django import app

from radis.reports.models import Report
from radis.reports.site import (
    ReportsCreatedHandler,
    ReportsUpdatedHandler,
    register_reports_created_handler,
    register_reports_updated_handler,
    reports_created_handlers,
    reports_updated_handlers,
)


logger = logging.getLogger("radis.labels")

HANDLER_NAME = "labels"


def _chunked(items: list[int], size: int):
    for i in range(0, len(items), size):
        yield items[i : i + size]


def _label_reports_handler(reports: list[Report]) -> None:
    if not reports:
        return
    deferrer = app.configure_task(
        "radis.labels.tasks.label_report_batch",
        allow_unknown=False,
        priority=settings.LABELING_INGEST_PRIORITY,
    )
    report_ids = [r.id for r in reports]
    for chunk in _chunked(report_ids, settings.LABELING_TASK_BATCH_SIZE):
        deferrer.defer(report_ids=chunk)


def register_report_handlers() -> None:
    if not any(h.name == HANDLER_NAME for h in reports_created_handlers):
        register_reports_created_handler(
            ReportsCreatedHandler(name=HANDLER_NAME, handle=_label_reports_handler)
        )
    if not any(h.name == HANDLER_NAME for h in reports_updated_handlers):
        register_reports_updated_handler(
            ReportsUpdatedHandler(name=HANDLER_NAME, handle=_label_reports_handler)
        )
```

- [ ] **Step 3: Test, commit**

```bash
uv run cli test -- radis/labels/tests/test_signals.py -q
git add radis/labels/signals.py radis/labels/tests/test_signals.py
git commit -m "feat(labels): handler chunks ingest batches and defers label_report_batch tasks"
```

---

# Phase 4 — Backfill path

## Task 16: `find_reports_needing_work`

**Files:**
- Modify: `radis/labels/services.py`
- Create: `radis/labels/tests/test_scope.py`

- [ ] **Step 1: Failing tests**

Create `radis/labels/tests/test_scope.py`:

```python
from datetime import timedelta

from radis.reports.factories import ReportFactory
from radis.labels.factories import AnswerFactory, QuestionFactory
from radis.labels.services import find_reports_needing_work


class TestFindReportsNeedingWork:
    def test_no_active_questions_means_empty_scope(self):
        ReportFactory()
        QuestionFactory(active=False)
        assert list(find_reports_needing_work(Report.objects.values_list("id", flat=True))) == []

    def test_report_with_no_answers_in_scope(self):
        r = ReportFactory()
        QuestionFactory(active=True)
        ids = list(find_reports_needing_work([r.id]))
        assert ids == [r.id]

    def test_report_with_all_current_answers_out_of_scope(self):
        r = ReportFactory()
        q1 = QuestionFactory(active=True)
        q2 = QuestionFactory(active=True)
        AnswerFactory(report=r, question=q1)
        AnswerFactory(report=r, question=q2)
        ids = list(find_reports_needing_work([r.id]))
        assert ids == []

    def test_report_with_stale_answer_in_scope(self):
        r = ReportFactory()
        q = QuestionFactory(active=True)
        a = AnswerFactory(report=r, question=q)
        type(q).objects.filter(pk=q.pk).update(
            updated_at=a.generated_at + timedelta(seconds=10)
        )
        ids = list(find_reports_needing_work([r.id]))
        assert ids == [r.id]
```

(Add necessary imports.)

- [ ] **Step 2: Implement**

Append to `radis/labels/services.py`:

```python
from typing import Iterable, Iterator

from django.db.models import Count, F, Q


def find_reports_needing_work(scope_ids: Iterable[int]) -> Iterator[int]:
    active_question_count = Question.objects.filter(active=True).count()
    if active_question_count == 0:
        return iter(())
    qs = (
        Report.objects.filter(id__in=scope_ids)
        .annotate(
            non_stale_count=Count(
                "answers",
                filter=Q(
                    answers__question__active=True,
                    answers__generated_at__gte=F("answers__question__updated_at"),
                ),
            )
        )
        .filter(non_stale_count__lt=active_question_count)
        .values_list("id", flat=True)
    )
    return qs.iterator()
```

- [ ] **Step 3: Test, commit**

```bash
uv run cli test -- radis/labels/tests/test_scope.py -q
git add radis/labels/services.py radis/labels/tests/test_scope.py
git commit -m "feat(labels): scope query for reports needing work"
```

---

## Task 17: `create_labeling_tasks_streaming`

**Files:**
- Modify: `radis/labels/services.py`, `radis/labels/tests/test_scope.py`

- [ ] **Step 1: Failing tests**

Append to `radis/labels/tests/test_scope.py`:

```python
from radis.core.models import AnalysisJob, AnalysisTask
from radis.labels.factories import LabelingJobFactory
from radis.labels.models import LabelingTask
from radis.labels.services import create_labeling_tasks_streaming


def test_create_labeling_tasks_streaming_buckets_by_batch_size(settings):
    settings.LABELING_TASK_BATCH_SIZE = 3
    job = LabelingJobFactory(status=AnalysisJob.Status.PREPARING)
    QuestionFactory(active=True)
    for _ in range(7):
        ReportFactory()
    create_labeling_tasks_streaming(job)
    tasks = list(LabelingTask.objects.filter(job=job).order_by("id"))
    assert [t.reports.count() for t in tasks] == [3, 3, 1]
    assert all(t.status == AnalysisTask.Status.PENDING for t in tasks)


def test_create_labeling_tasks_streaming_aborts_on_cancel(settings):
    settings.LABELING_TASK_BATCH_SIZE = 2
    job = LabelingJobFactory(status=AnalysisJob.Status.PREPARING)
    QuestionFactory(active=True)
    for _ in range(10):
        ReportFactory()

    from radis.labels import services as services_mod

    real_flush = services_mod._flush_bucket

    def cancel_after_first(job, ids):
        real_flush(job, ids)
        type(job).objects.filter(pk=job.pk).update(
            status=AnalysisJob.Status.CANCELING
        )

    from unittest.mock import patch
    with patch.object(services_mod, "_flush_bucket", side_effect=cancel_after_first):
        create_labeling_tasks_streaming(job)
    # Only the first bucket got flushed before cancellation was observed.
    assert LabelingTask.objects.filter(job=job).count() == 1
```

- [ ] **Step 2: Implement**

Append to `radis/labels/services.py`:

```python
from django.db import transaction

from radis.core.models import AnalysisJob, AnalysisTask
from .models import LabelingJob, LabelingTask


def create_labeling_tasks_streaming(job: LabelingJob) -> int:
    from django.conf import settings

    batch_size = settings.LABELING_TASK_BATCH_SIZE
    total = 0
    bucket: list[int] = []

    scope_iter = find_reports_needing_work(
        Report.objects.order_by("pk").values_list("id", flat=True)
    )

    for report_id in scope_iter:
        bucket.append(report_id)
        if len(bucket) >= batch_size:
            _flush_bucket(job, bucket)
            total += 1
            bucket = []
            # Cancellation check between buckets.
            current_status = (
                LabelingJob.objects.filter(pk=job.pk)
                .values_list("status", flat=True)
                .first()
            )
            if current_status == AnalysisJob.Status.CANCELING:
                return total

    if bucket:
        _flush_bucket(job, bucket)
        total += 1

    return total


def _flush_bucket(job: LabelingJob, report_ids: list[int]) -> None:
    with transaction.atomic():
        task = LabelingTask.objects.create(
            job=job, status=AnalysisTask.Status.PENDING
        )
        task.reports.set(report_ids)
```

- [ ] **Step 3: Test, commit**

```bash
uv run cli test -- radis/labels/tests/test_scope.py -q
git add radis/labels/services.py radis/labels/tests/test_scope.py
git commit -m "feat(labels): stream-create LabelingTasks with between-chunk cancel check"
```

---

## Task 18: `LabelingTaskProcessor`

**Files:**
- Create: `radis/labels/processors.py`
- Create: `radis/labels/tests/test_processor.py`

- [ ] **Step 1: Failing tests**

Create `radis/labels/tests/test_processor.py`:

```python
from unittest.mock import patch

from radis.core.models import AnalysisJob, AnalysisTask
from radis.reports.factories import ReportFactory
from radis.labels.factories import LabelingJobFactory, LabelingTaskFactory, QuestionFactory
from radis.labels.models import Answer
from radis.labels.processors import LabelingTaskProcessor


def _task_with_reports(n_reports=3):
    job = LabelingJobFactory(status=AnalysisJob.Status.PENDING)
    task = LabelingTaskFactory(job=job, status=AnalysisTask.Status.PENDING)
    for _ in range(n_reports):
        task.reports.add(ReportFactory(body="b"))
    return task


class TestLabelingTaskProcessor:
    def test_success_when_all_succeed(self):
        task = _task_with_reports(3)
        QuestionFactory(active=True, group="g")
        with patch("radis.labels.services.ChatClient") as ChatClientMock:
            ChatClientMock.return_value.extract_data.side_effect = (
                lambda p, S: S(**{f: "YES" for f in S.model_fields})
            )
            LabelingTaskProcessor(task).start()
        task.refresh_from_db()
        assert task.status == AnalysisTask.Status.SUCCESS
        assert Answer.objects.count() == 3

    def test_warning_on_partial_failure(self):
        task = _task_with_reports(2)
        report_ids = list(task.reports.values_list("id", flat=True))
        QuestionFactory(active=True, group="g")
        from radis.reports.models import Report

        bad_id = report_ids[0]
        bad_body = Report.objects.get(id=bad_id).body
        def fake(prompt, Schema):
            if bad_body in prompt:
                raise RuntimeError("bad")
            return Schema(**{f: "YES" for f in Schema.model_fields})
        with patch("radis.labels.services.ChatClient") as ChatClientMock:
            ChatClientMock.return_value.extract_data.side_effect = fake
            LabelingTaskProcessor(task).start()
        task.refresh_from_db()
        assert task.status == AnalysisTask.Status.WARNING

    def test_failure_when_all_fail(self):
        task = _task_with_reports(2)
        QuestionFactory(active=True, group="g")
        with patch("radis.labels.services.ChatClient") as ChatClientMock:
            ChatClientMock.return_value.extract_data.side_effect = RuntimeError
            LabelingTaskProcessor(task).start()
        task.refresh_from_db()
        assert task.status == AnalysisTask.Status.FAILURE
```

- [ ] **Step 2: Implement**

Create `radis/labels/processors.py`:

```python
import logging

from radis.core.models import AnalysisTask
from radis.core.processors import AnalysisTaskProcessor

from .models import LabelingTask
from .services import label_reports_in_parallel


logger = logging.getLogger("radis.labels")


class LabelingTaskProcessor(AnalysisTaskProcessor):
    def process_task(self, task: LabelingTask) -> None:
        report_ids = list(task.reports.values_list("id", flat=True))
        success, failure = label_reports_in_parallel(report_ids)

        if failure == 0:
            task.status = AnalysisTask.Status.SUCCESS
            task.message = ""
        elif success == 0:
            task.status = AnalysisTask.Status.FAILURE
            task.message = f"All {failure} report labelings failed."
        else:
            task.status = AnalysisTask.Status.WARNING
            task.message = f"{failure} of {success + failure} report labelings failed."
```

Implementation note: verify against `radis/core/processors.py` whether `AnalysisTaskProcessor.start()` overrides `task.status` after `process_task` returns. If it does, store the result on `task.log`/`task.message` only and let the base class set status; adjust accordingly while keeping the tests passing.

- [ ] **Step 3: Test, commit**

```bash
uv run cli test -- radis/labels/tests/test_processor.py -q
git add radis/labels/processors.py radis/labels/tests/test_processor.py
git commit -m "feat(labels): LabelingTaskProcessor with SUCCESS/WARNING/FAILURE mapping"
```

---

## Task 19: `process_labeling_task` Procrastinate task

**Files:**
- Modify: `radis/labels/tasks.py`, `radis/labels/tests/test_tasks.py`

- [ ] **Step 1: Failing tests**

Append to `radis/labels/tests/test_tasks.py`:

```python
from radis.labels.factories import LabelingTaskFactory
from radis.labels.tasks import process_labeling_task


def test_process_labeling_task_invokes_processor():
    task = LabelingTaskFactory()
    with patch("radis.labels.tasks.LabelingTaskProcessor") as ProcessorMock:
        process_labeling_task(task_id=task.id)
    ProcessorMock.assert_called_once()
    ProcessorMock.return_value.start.assert_called_once()
```

- [ ] **Step 2: Implement**

Append to `radis/labels/tasks.py`:

```python
from .models import LabelingTask
from .processors import LabelingTaskProcessor


@app.task(queue="llm")
def process_labeling_task(task_id: int) -> None:
    task = LabelingTask.objects.get(id=task_id)
    LabelingTaskProcessor(task).start()
    task.queued_job_id = None
    task.save()
```

- [ ] **Step 3: Test, commit**

```bash
uv run cli test -- radis/labels/tests/test_tasks.py -q
git add radis/labels/tasks.py radis/labels/tests/test_tasks.py
git commit -m "feat(labels): process_labeling_task Procrastinate task"
```

---

## Task 20: `process_labeling_job` orchestrator + `LabelingJob.delay`

**Files:**
- Modify: `radis/labels/tasks.py`, `radis/labels/models.py`, `radis/labels/tests/test_tasks.py`

- [ ] **Step 1: Failing tests**

Append to `radis/labels/tests/test_tasks.py`:

```python
from radis.core.models import AnalysisJob
from radis.labels.factories import LabelingJobFactory
from radis.labels.tasks import process_labeling_job


class TestProcessLabelingJob:
    def test_prep_then_pending(self):
        job = LabelingJobFactory(status=AnalysisJob.Status.UNVERIFIED)
        QuestionFactory(active=True)
        for _ in range(2):
            ReportFactory()
        with patch("radis.labels.tasks.enqueue_all_pending_tasks"):
            process_labeling_job(job_id=job.id)
        job.refresh_from_db()
        assert job.status == AnalysisJob.Status.PENDING
        assert job.started_at is not None

    def test_deletes_pre_existing_tasks(self):
        job = LabelingJobFactory(status=AnalysisJob.Status.UNVERIFIED)
        # Simulate a crashed prior attempt: pre-existing tasks under this job.
        from radis.labels.factories import LabelingTaskFactory
        LabelingTaskFactory(job=job)
        LabelingTaskFactory(job=job)
        QuestionFactory(active=True)
        for _ in range(2):
            ReportFactory()
        with patch("radis.labels.tasks.enqueue_all_pending_tasks"):
            process_labeling_job(job_id=job.id)
        # Pre-existing tasks were deleted; new ones created for the 2 reports.
        # With default batch size 100 that's 1 new task.
        assert job.tasks.count() == 1
```

- [ ] **Step 2: Implement the orchestrator**

Append to `radis/labels/tasks.py`:

```python
from django.utils import timezone

from radis.core.models import AnalysisJob, AnalysisTask
from .models import LabelingJob
from .services import create_labeling_tasks_streaming


@app.task()
def process_labeling_job(job_id: int) -> None:
    job = LabelingJob.objects.get(id=job_id)
    job.tasks.all().delete()  # restart-safe under retry
    job.status = AnalysisJob.Status.PREPARING
    job.started_at = timezone.now()
    job.save()

    create_labeling_tasks_streaming(job)

    job.refresh_from_db()
    if job.status == AnalysisJob.Status.CANCELING:
        # PREPARING was cancelled mid-stream; the cancel admin view will finalize state.
        return

    job.status = AnalysisJob.Status.PENDING
    job.save()

    enqueue_all_pending_tasks(job)


def enqueue_all_pending_tasks(job: LabelingJob) -> None:
    """Implemented in Task 21."""
    raise NotImplementedError("Implemented in Task 21")
```

- [ ] **Step 3: Implement `LabelingJob.delay`**

In `radis/labels/models.py`, replace the `delay` body in `LabelingJob`:

```python
    def delay(self) -> None:
        from procrastinate.contrib.django import app

        queued_job_id = app.configure_task(
            "radis.labels.tasks.process_labeling_job",
            allow_unknown=False,
            priority=self.default_priority,
        ).defer(job_id=self.pk)
        self.queued_job_id = queued_job_id
        self.save()
```

- [ ] **Step 4: Test, commit**

```bash
uv run cli test -- radis/labels/tests/test_tasks.py::TestProcessLabelingJob -q
git add radis/labels/tasks.py radis/labels/models.py radis/labels/tests/test_tasks.py
git commit -m "feat(labels): process_labeling_job (restart-safe) and LabelingJob.delay"
```

---

## Task 21: `enqueue_all_pending_tasks`

**Files:**
- Modify: `radis/labels/tasks.py`, `radis/labels/tests/test_tasks.py`

- [ ] **Step 1: Failing tests**

Append to `radis/labels/tests/test_tasks.py`:

```python
from radis.labels.factories import LabelingTaskFactory
from radis.labels.tasks import enqueue_all_pending_tasks


def test_enqueue_defers_one_procrastinate_job_per_pending_task():
    job = LabelingJobFactory(status=AnalysisJob.Status.PENDING)
    t1 = LabelingTaskFactory(job=job, status=AnalysisTask.Status.PENDING)
    t2 = LabelingTaskFactory(job=job, status=AnalysisTask.Status.PENDING)
    LabelingTaskFactory(job=job, status=AnalysisTask.Status.SUCCESS)  # skipped
    with patch("radis.labels.tasks.app") as app_mock:
        deferrer = app_mock.configure_task.return_value
        deferrer.defer.side_effect = [101, 102]
        enqueue_all_pending_tasks(job)
    assert deferrer.defer.call_count == 2
    deferred_ids = sorted(c.kwargs["task_id"] for c in deferrer.defer.call_args_list)
    assert deferred_ids == sorted([t1.id, t2.id])
```

- [ ] **Step 2: Implement**

Replace the stub in `radis/labels/tasks.py`:

```python
def enqueue_all_pending_tasks(job: LabelingJob) -> None:
    pending = job.tasks.filter(status=AnalysisTask.Status.PENDING)
    deferrer = app.configure_task(
        "radis.labels.tasks.process_labeling_task",
        allow_unknown=False,
        priority=job.default_priority,
    )
    for task in pending.iterator(chunk_size=500):
        queued_job_id = deferrer.defer(task_id=task.id)
        task.queued_job_id = queued_job_id
        task.save(update_fields=["queued_job_id"])
```

- [ ] **Step 3: Test, commit**

```bash
uv run cli test -- radis/labels/tests/test_tasks.py -q
git add radis/labels/tasks.py radis/labels/tests/test_tasks.py
git commit -m "feat(labels): enqueue_all_pending_tasks"
```

---

# Phase 5 — Admin UX

## Task 22: `QuestionAdmin`

**Files:**
- Create: `radis/labels/admin.py`
- Create: `radis/labels/tests/test_admin.py`

- [ ] **Step 1: Failing tests**

Create `radis/labels/tests/test_admin.py`:

```python
import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse

from radis.labels.factories import QuestionFactory


User = get_user_model()


@pytest.fixture
def admin_client(client):
    user = User.objects.create_superuser(
        username="admin", email="admin@example.com", password="pw"
    )
    client.force_login(user)
    return client


class TestQuestionAdmin:
    def test_changelist_loads(self, admin_client):
        QuestionFactory(label="pneumonia")
        resp = admin_client.get(reverse("admin:labels_question_changelist"))
        assert resp.status_code == 200
        assert b"pneumonia" in resp.content

    def test_add_form_loads(self, admin_client):
        resp = admin_client.get(reverse("admin:labels_question_add"))
        assert resp.status_code == 200

    def test_duplicate_label_rejected(self, admin_client):
        QuestionFactory(label="pneumonia")
        resp = admin_client.post(
            reverse("admin:labels_question_add"),
            data={"label": "pneumonia", "text": "q?", "group": "g", "active": "on"},
        )
        assert resp.status_code == 200
        assert b"already exists" in resp.content.lower() or b"unique" in resp.content.lower()
```

- [ ] **Step 2: Implement**

Create `radis/labels/admin.py`:

```python
from django.contrib import admin
from django.db.models import Count, Q
from django.utils.html import format_html

from .models import Question


@admin.register(Question)
class QuestionAdmin(admin.ModelAdmin):
    list_display = ("label", "group", "active", "text_preview", "updated_at", "answer_summary")
    list_filter = ("active", "group")
    search_fields = ("label", "group", "text")
    ordering = ("group", "label")
    readonly_fields = ("created_at", "updated_at", "answer_summary")
    fieldsets = (
        (None, {"fields": ("label", "group", "text", "active")}),
        ("Stats", {"fields": ("answer_summary", "created_at", "updated_at")}),
    )

    def text_preview(self, obj: Question) -> str:
        if not obj.text:
            return ""
        return obj.text[:80] + ("…" if len(obj.text) > 80 else "")
    text_preview.short_description = "Question"

    def answer_summary(self, obj: Question) -> str:
        if obj.pk is None:
            return "—"
        counts = obj.answers.aggregate(
            yes=Count("pk", filter=Q(value="YES")),
            no=Count("pk", filter=Q(value="NO")),
            maybe=Count("pk", filter=Q(value="MAYBE")),
            stale=Count("pk", filter=Q(generated_at__lt=obj.updated_at)),
        )
        return format_html(
            "{yes} Yes · {maybe} Maybe · {no} No · {stale} stale", **counts
        )
    answer_summary.short_description = "Answers"
```

- [ ] **Step 3: Test, commit**

```bash
uv run cli test -- radis/labels/tests/test_admin.py::TestQuestionAdmin -q
git add radis/labels/admin.py radis/labels/tests/test_admin.py
git commit -m "feat(labels): QuestionAdmin with answer summary"
```

---

## Task 23: `AnswerAdmin` (read-only)

**Files:**
- Modify: `radis/labels/admin.py`, `radis/labels/tests/test_admin.py`

- [ ] **Step 1: Failing tests**

Append to `radis/labels/tests/test_admin.py`:

```python
from radis.labels.factories import AnswerFactory


class TestAnswerAdmin:
    def test_changelist_loads(self, admin_client):
        AnswerFactory()
        resp = admin_client.get(reverse("admin:labels_answer_changelist"))
        assert resp.status_code == 200

    def test_add_disabled(self, admin_client):
        resp = admin_client.get(reverse("admin:labels_answer_add"))
        assert resp.status_code in (302, 403)
```

- [ ] **Step 2: Implement**

Append to `radis/labels/admin.py`:

```python
from django.db.models import F

from .models import Answer


class IsStaleFilter(admin.SimpleListFilter):
    title = "stale"
    parameter_name = "is_stale"

    def lookups(self, request, model_admin):
        return [("1", "Stale"), ("0", "Current")]

    def queryset(self, request, queryset):
        if self.value() == "1":
            return queryset.filter(generated_at__lt=F("question__updated_at"))
        if self.value() == "0":
            return queryset.filter(generated_at__gte=F("question__updated_at"))
        return queryset


@admin.register(Answer)
class AnswerAdmin(admin.ModelAdmin):
    list_display = ("report", "question_label", "value", "is_stale", "generated_at")
    list_filter = ("value", "question__group", "question", IsStaleFilter)
    search_fields = ("report__document_id", "question__label")
    raw_id_fields = ("report", "question")
    readonly_fields = tuple(f.name for f in Answer._meta.fields)

    def question_label(self, obj: Answer) -> str:
        return obj.question.label
    question_label.short_description = "Label"

    def is_stale(self, obj: Answer) -> bool:
        return obj.generated_at < obj.question.updated_at
    is_stale.boolean = True

    def has_add_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False
```

- [ ] **Step 3: Test, commit**

```bash
uv run cli test -- radis/labels/tests/test_admin.py::TestAnswerAdmin -q
git add radis/labels/admin.py radis/labels/tests/test_admin.py
git commit -m "feat(labels): read-only AnswerAdmin with stale filter"
```

---

## Task 24: `AnswerInline` on `ReportAdmin`

**Files:**
- Modify: `radis/labels/admin.py`, `radis/reports/admin.py`, `radis/labels/tests/test_admin.py`

- [ ] **Step 1: Failing tests**

Append to `radis/labels/tests/test_admin.py`:

```python
from radis.reports.factories import ReportFactory


def test_report_change_shows_answer_inline(admin_client):
    r = ReportFactory()
    AnswerFactory(report=r)
    resp = admin_client.get(reverse("admin:reports_report_change", args=[r.id]))
    assert resp.status_code == 200
    assert b"answer" in resp.content.lower()
```

- [ ] **Step 2: Define the inline**

Append to `radis/labels/admin.py`:

```python
class AnswerInline(admin.TabularInline):
    model = Answer
    fields = ("question", "value", "generated_at")
    readonly_fields = fields
    extra = 0
    can_delete = False
    show_change_link = False
```

- [ ] **Step 3: Wire into `ReportAdmin`**

Open `radis/reports/admin.py`; find the existing `ReportAdmin` class and its `inlines` list. Add:

```python
from radis.labels.admin import AnswerInline
```

at the top, and append `AnswerInline` to the existing `inlines` list.

- [ ] **Step 4: Test, commit**

```bash
uv run cli test -- radis/labels/tests/test_admin.py -q
git add radis/labels/admin.py radis/reports/admin.py radis/labels/tests/test_admin.py
git commit -m "feat(labels): AnswerInline on ReportAdmin"
```

---

## Task 25: `LabelingJobAdmin` (read-only list + detail)

**Files:**
- Modify: `radis/labels/admin.py`, `radis/labels/tests/test_admin.py`

- [ ] **Step 1: Failing tests**

Append to `radis/labels/tests/test_admin.py`:

```python
from radis.core.models import AnalysisJob
from radis.labels.factories import LabelingJobFactory


class TestLabelingJobAdmin:
    def test_changelist_loads(self, admin_client):
        LabelingJobFactory(status=AnalysisJob.Status.PENDING)
        resp = admin_client.get(reverse("admin:labels_labelingjob_changelist"))
        assert resp.status_code == 200

    def test_add_disabled(self, admin_client):
        resp = admin_client.get(reverse("admin:labels_labelingjob_add"))
        assert resp.status_code in (302, 403)
```

- [ ] **Step 2: Implement**

Append to `radis/labels/admin.py`:

```python
from radis.core.models import AnalysisTask
from .models import LabelingJob


@admin.register(LabelingJob)
class LabelingJobAdmin(admin.ModelAdmin):
    list_display = (
        "id", "status", "owner", "progress_detail",
        "created_at", "started_at", "ended_at",
    )
    list_filter = ("status",)
    readonly_fields = (
        "status", "owner", "message", "created_at", "started_at", "ended_at",
        "progress_detail",
    )
    fields = readonly_fields

    def progress_detail(self, obj: LabelingJob) -> str:
        if obj.pk is None:
            return "—"
        statuses = list(obj.tasks.values_list("status", flat=True))
        total = len(statuses)
        if total == 0:
            return "No tasks yet."
        s = sum(1 for x in statuses if x == AnalysisTask.Status.SUCCESS)
        w = sum(1 for x in statuses if x == AnalysisTask.Status.WARNING)
        f = sum(1 for x in statuses if x == AnalysisTask.Status.FAILURE)
        p = sum(1 for x in statuses if x == AnalysisTask.Status.PENDING)
        ip = sum(1 for x in statuses if x == AnalysisTask.Status.IN_PROGRESS)
        return (
            f"{s} success · {w} warning · {f} failure · "
            f"{ip} in-progress · {p} pending · {total} total"
        )

    def has_add_permission(self, request):
        return False
```

- [ ] **Step 3: Test, commit**

```bash
uv run cli test -- radis/labels/tests/test_admin.py::TestLabelingJobAdmin -q
git add radis/labels/admin.py radis/labels/tests/test_admin.py
git commit -m "feat(labels): LabelingJobAdmin (read-only with progress summary)"
```

---

## Task 26: Run/Cancel admin views + changelist banner

**Files:**
- Create: `radis/labels/admin_views.py`
- Modify: `radis/labels/admin.py`
- Create: `radis/labels/templates/labels/admin/labelingjob_changelist.html`
- Modify: `radis/labels/tests/test_admin.py`

- [ ] **Step 1: Failing tests**

Append to `radis/labels/tests/test_admin.py`:

```python
class TestRunCancelBackfill:
    def test_run_creates_job_when_none_active(self, admin_client):
        with patch("radis.labels.admin_views.LabelingJob.delay"):
            resp = admin_client.post(reverse("admin:labels_run_backfill"))
        assert resp.status_code in (302, 303)
        # A new job exists.
        from radis.labels.models import LabelingJob
        assert LabelingJob.objects.count() == 1

    def test_run_rejected_when_one_active(self, admin_client):
        LabelingJobFactory(status=AnalysisJob.Status.PENDING)
        resp = admin_client.post(reverse("admin:labels_run_backfill"), follow=True)
        assert resp.status_code == 200
        assert (b"another" in resp.content.lower()
                or b"already active" in resp.content.lower())

    def test_cancel_sets_canceling(self, admin_client):
        job = LabelingJobFactory(status=AnalysisJob.Status.IN_PROGRESS)
        resp = admin_client.post(reverse("admin:labels_cancel_backfill", args=[job.id]))
        assert resp.status_code in (302, 303)
        job.refresh_from_db()
        assert job.status == AnalysisJob.Status.CANCELING
```

- [ ] **Step 2: Implement admin views**

Create `radis/labels/admin_views.py`:

```python
from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.db import IntegrityError, transaction
from django.http import HttpRequest, HttpResponseRedirect
from django.shortcuts import get_object_or_404
from django.urls import reverse
from django.views.decorators.http import require_POST

from radis.core.models import AnalysisJob
from .models import LabelingJob


@staff_member_required
@require_POST
def run_backfill_view(request: HttpRequest) -> HttpResponseRedirect:
    target = reverse("admin:labels_labelingjob_changelist")
    if LabelingJob.objects.filter(status__in=LabelingJob.ACTIVE_STATUSES).exists():
        messages.error(request, "Another backfill is already active.")
        return HttpResponseRedirect(target)
    try:
        with transaction.atomic():
            job = LabelingJob.objects.create(
                owner=request.user, status=AnalysisJob.Status.UNVERIFIED
            )
        job.delay()
        messages.success(request, f"Backfill job #{job.id} started.")
    except IntegrityError:
        messages.error(request, "Another backfill just started; please refresh.")
    return HttpResponseRedirect(target)


@staff_member_required
@require_POST
def cancel_backfill_view(request: HttpRequest, job_id: int) -> HttpResponseRedirect:
    target = reverse("admin:labels_labelingjob_changelist")
    job = get_object_or_404(LabelingJob, id=job_id)
    if job.status not in LabelingJob.ACTIVE_STATUSES:
        messages.error(request, "Job is not in a cancelable state.")
    else:
        job.status = AnalysisJob.Status.CANCELING
        job.save(update_fields=["status"])
        messages.success(request, f"Backfill job #{job.id} canceling.")
    return HttpResponseRedirect(target)
```

- [ ] **Step 3: Wire URLs into `LabelingJobAdmin`**

In `radis/labels/admin.py`, modify `LabelingJobAdmin` to add URLs, the changelist template, and the changelist context for the banner:

```python
from django.urls import path

from . import admin_views


# Inside LabelingJobAdmin:
    change_list_template = "labels/admin/labelingjob_changelist.html"

    def get_urls(self):
        urls = super().get_urls()
        custom = [
            path(
                "run/",
                self.admin_site.admin_view(admin_views.run_backfill_view),
                name="labels_run_backfill",
            ),
            path(
                "<int:job_id>/cancel/",
                self.admin_site.admin_view(admin_views.cancel_backfill_view),
                name="labels_cancel_backfill",
            ),
        ]
        return custom + urls

    def changelist_view(self, request, extra_context=None):
        extra_context = extra_context or {}
        extra_context["active_job"] = (
            LabelingJob.objects.filter(status__in=LabelingJob.ACTIVE_STATUSES).first()
        )
        return super().changelist_view(request, extra_context=extra_context)
```

- [ ] **Step 4: Create the changelist template**

Create `radis/labels/templates/labels/admin/labelingjob_changelist.html`:

```html
{% extends "admin/change_list.html" %}

{% block content_title %}
  {{ block.super }}
  <div style="margin: 1em 0; padding: 1em; background: #f8f9fa; border-radius: 4px;">
    {% if active_job %}
      <strong>Active backfill #{{ active_job.id }}</strong>
      — status: {{ active_job.get_status_display }}
      [<a href="{% url 'admin:labels_labelingjob_change' active_job.id %}">view</a>]
      <form method="post"
            action="{% url 'admin:labels_cancel_backfill' active_job.id %}"
            style="display: inline; margin-left: 1em;">
        {% csrf_token %}
        <button type="submit" class="button"
                onclick="return confirm('Cancel this backfill?');">Cancel</button>
      </form>
    {% else %}
      <form method="post" action="{% url 'admin:labels_run_backfill' %}">
        {% csrf_token %}
        <button type="submit" class="button"
                onclick="return confirm('Start a new backfill?');">Run backfill</button>
      </form>
    {% endif %}
  </div>
{% endblock %}
```

- [ ] **Step 5: Test, commit**

```bash
uv run cli test -- radis/labels/tests/test_admin.py::TestRunCancelBackfill -q
git add radis/labels/admin.py radis/labels/admin_views.py radis/labels/templates/ radis/labels/tests/test_admin.py
git commit -m "feat(labels): Run/Cancel backfill via admin changelist banner"
```

---

# Phase 6 — Report detail surfacing

## Task 27: Cotton component `<c-report-labels />`

**Files:**
- Create: `radis/labels/templates/cotton/report_labels.html`
- Create: `radis/labels/tests/test_cotton.py`

- [ ] **Step 1: Failing tests**

Create `radis/labels/tests/test_cotton.py`:

```python
from datetime import timedelta

from django.template import Context, Template
from django.utils import timezone

from radis.reports.factories import ReportFactory
from radis.labels.factories import AnswerFactory, QuestionFactory
from radis.labels.models import Answer, Question


def _render(report):
    tmpl = Template('{% load cotton %}<c-report-labels :report="report" />')
    return tmpl.render(Context({"report": report}))


class TestReportLabelsComponent:
    def test_yes_badge(self):
        r = ReportFactory()
        q = QuestionFactory(label="pneumonia", group="lung")
        AnswerFactory(report=r, question=q, value="YES")
        out = _render(r)
        assert "pneumonia" in out

    def test_maybe_marked_distinctly(self):
        r = ReportFactory()
        q = QuestionFactory(label="pneumonia", group="lung")
        AnswerFactory(report=r, question=q, value="MAYBE")
        out = _render(r)
        assert "pneumonia" in out
        assert "?" in out or "maybe" in out.lower()

    def test_no_value_not_rendered(self):
        r = ReportFactory()
        q = QuestionFactory(label="not-applicable", group="lung")
        AnswerFactory(report=r, question=q, value="NO")
        out = _render(r)
        assert "not-applicable" not in out

    def test_stale_styling(self):
        r = ReportFactory()
        q = QuestionFactory(label="pneumonia", group="lung")
        a = AnswerFactory(report=r, question=q, value="YES")
        Question.objects.filter(pk=q.pk).update(
            updated_at=a.generated_at + timedelta(seconds=10)
        )
        out = _render(r)
        assert "stale" in out.lower() or "outdated" in out.lower()

    def test_no_answers_shows_pending(self):
        r = ReportFactory()
        out = _render(r)
        assert "pending" in out.lower()
```

- [ ] **Step 2: Implement the component**

Create `radis/labels/templates/cotton/report_labels.html`:

```html
<div class="report-labels">
  {% with answers=report.answers.all %}
    {% if not answers %}
      <p class="text-muted small">Labels pending.</p>
    {% else %}
      {% regroup answers|dictsort:"question.group" by question.group as groups %}
      {% for group in groups %}
        <div class="report-labels-group mb-2">
          <h6 class="text-muted">{{ group.grouper }}</h6>
          {% for answer in group.list %}
            {% if answer.value != "NO" %}
              {% if answer.generated_at < answer.question.updated_at %}
                <span class="badge bg-secondary"
                      title="label may be outdated; will be refreshed by next backfill">
                  {{ answer.question.label }} <small>(stale)</small>
                </span>
              {% elif answer.value == "MAYBE" %}
                <span class="badge bg-warning text-dark" title="Maybe">
                  {{ answer.question.label }} ?
                </span>
              {% else %}
                <span class="badge bg-success" title="Yes">
                  {{ answer.question.label }}
                </span>
              {% endif %}
            {% endif %}
          {% endfor %}
        </div>
      {% endfor %}
    {% endif %}
  {% endwith %}
</div>
```

Implementation note: confirm the Cotton component naming convention. If existing components in `radis/core/templates/cotton/` use `kebab_case.html` to resolve to `<c-kebab-case />`, this file's name matches. Verify by inspecting one existing component before committing.

- [ ] **Step 3: Test, commit**

```bash
uv run cli test -- radis/labels/tests/test_cotton.py -q
git add radis/labels/templates/cotton/ radis/labels/tests/test_cotton.py
git commit -m "feat(labels): <c-report-labels /> Cotton component"
```

---

## Task 28: Wire into report detail + prefetch

**Files:**
- Modify: `radis/reports/templates/reports/report_detail.html`
- Modify: `radis/reports/views.py`
- Modify: `radis/labels/tests/test_cotton.py`

- [ ] **Step 1: Failing test**

Append to `radis/labels/tests/test_cotton.py`:

```python
from django.urls import reverse


def test_report_detail_renders_labels(admin_client):
    r = ReportFactory()
    q = QuestionFactory(label="pneumonia", group="lung")
    AnswerFactory(report=r, question=q, value="YES")
    resp = admin_client.get(reverse("report_detail", kwargs={"pk": r.id}))
    assert resp.status_code == 200
    assert b"pneumonia" in resp.content
```

- [ ] **Step 2: Include component on detail template**

Edit `radis/reports/templates/reports/report_detail.html`: after the line that includes `_report_buttons_panel.html` (around line 71), add:

```html
<c-report-labels :report="report" />
```

- [ ] **Step 3: Prefetch on detail view**

In `radis/reports/views.py`, find `ReportDetailView` and add (or extend) `get_queryset`:

```python
from django.db.models import Prefetch


def get_queryset(self):
    from radis.labels.models import Answer

    return (
        super()
        .get_queryset()
        .prefetch_related(
            Prefetch(
                "answers",
                queryset=Answer.objects.exclude(value="NO")
                .select_related("question")
                .order_by("question__group", "question__label"),
            )
        )
    )
```

- [ ] **Step 4: Test, commit**

```bash
uv run cli test -- radis/labels/tests/test_cotton.py -q
git add radis/reports/templates/reports/report_detail.html radis/reports/views.py radis/labels/tests/test_cotton.py
git commit -m "feat(labels): show labels on the report detail page (with prefetch)"
```

---

# Phase 7 — Search surfacing (multi-select)

## Task 29: Add `labels` to `SearchFilters` and `SearchForm`

**Files:**
- Modify: `radis/search/` (`models.py` / `forms.py` — actual filenames per your search app)
- Create: `radis/labels/tests/test_search_integration.py`

**Note before writing:** locate the `SearchFilters` dataclass. Grep:

```bash
grep -rn "class SearchFilters" radis/search/ radis/pgsearch/
```

Apply edits in the file that defines it. The shape below is a sketch; mirror the dataclass style already used.

- [ ] **Step 1: Failing tests**

Create `radis/labels/tests/test_search_integration.py`:

```python
import pytest

from radis.reports.factories import ReportFactory
from radis.labels.factories import AnswerFactory, QuestionFactory


@pytest.fixture
def labeled_corpus():
    r1, r2, r3 = ReportFactory(), ReportFactory(), ReportFactory()
    q_pneu = QuestionFactory(label="pneumonia", group="lung")
    q_eff = QuestionFactory(label="effusion", group="lung")
    AnswerFactory(report=r1, question=q_pneu, value="YES")
    AnswerFactory(report=r1, question=q_eff, value="NO")
    AnswerFactory(report=r2, question=q_pneu, value="MAYBE")
    AnswerFactory(report=r3, question=q_eff, value="YES")
    return {"r1": r1, "r2": r2, "r3": r3}


class TestSearchFiltersCarryLabels:
    def test_labels_field_default_empty(self):
        from radis.search.models import SearchFilters  # adjust import to actual path

        assert SearchFilters().labels == []

    def test_labels_roundtrips(self):
        from radis.search.models import SearchFilters

        assert SearchFilters(labels=["pneumonia"]).labels == ["pneumonia"]
```

- [ ] **Step 2: Add the field**

In `SearchFilters` (whichever module/file), add `labels: list[str] = field(default_factory=list)`.

In the SearchForm, add:

```python
from radis.labels.models import Question


labels = forms.MultipleChoiceField(
    required=False,
    widget=forms.CheckboxSelectMultiple,
    choices=[],
)

def __init__(self, *args, **kwargs):
    super().__init__(*args, **kwargs)
    self.fields["labels"].choices = [
        (label, label)
        for label in Question.objects.filter(active=True)
        .order_by("label")
        .values_list("label", flat=True)
        .distinct()
    ]
```

Render the field inside the existing Filters card (the crispy `filters_helper` layout). Pattern match the existing field layouts.

- [ ] **Step 3: Test, commit**

```bash
uv run cli test -- radis/labels/tests/test_search_integration.py::TestSearchFiltersCarryLabels -q
git add radis/search/ radis/labels/tests/test_search_integration.py
git commit -m "feat(labels): add labels filter to SearchFilters and SearchForm"
```

---

## Task 30: pgsearch translator + `facet_label_counts`

**Files:**
- Modify: `radis/pgsearch/providers.py`, `radis/labels/tests/test_search_integration.py`

- [ ] **Step 1: Failing tests**

Append to `radis/labels/tests/test_search_integration.py`:

```python
class TestLabelFilterTranslation:
    def test_single_label_filter(self, labeled_corpus):
        from radis.reports.models import Report
        from radis.search.models import SearchFilters
        from radis.pgsearch.providers import _build_filter_query

        q = _build_filter_query(SearchFilters(labels=["pneumonia"]))
        matched = set(Report.objects.filter(q).values_list("id", flat=True))
        assert labeled_corpus["r1"].id in matched   # YES
        assert labeled_corpus["r2"].id in matched   # MAYBE
        assert labeled_corpus["r3"].id not in matched

    def test_and_across_labels(self, labeled_corpus):
        from radis.reports.models import Report
        from radis.search.models import SearchFilters
        from radis.pgsearch.providers import _build_filter_query

        q = _build_filter_query(SearchFilters(labels=["pneumonia", "effusion"]))
        # r1: YES pneumonia, NO effusion → excluded
        # r2: MAYBE pneumonia, no effusion answer → excluded
        # r3: no pneumonia answer, YES effusion → excluded
        assert list(Report.objects.filter(q).values_list("id", flat=True)) == []


class TestFacetCounts:
    def test_counts(self, labeled_corpus):
        from radis.reports.models import Report
        from radis.pgsearch.providers import facet_label_counts

        rqs = Report.objects.all()
        d = dict(facet_label_counts(rqs, top_n=10))
        assert d.get("pneumonia") == 2
        assert d.get("effusion") == 1

    def test_top_n(self, labeled_corpus):
        from radis.reports.models import Report
        from radis.pgsearch.providers import facet_label_counts

        assert len(facet_label_counts(Report.objects.all(), top_n=1)) == 1
```

- [ ] **Step 2: Implement**

In `radis/pgsearch/providers.py`, extend `_build_filter_query`. Verify whether the function returns a Q targeting `Report` directly or `ReportSearchVector` — adjust the `id__in=` or `report__id__in=` accordingly to match neighbouring blocks:

```python
if filters.labels:
    from radis.labels.models import Answer
    for label in filters.labels:
        fq &= Q(
            id__in=Answer.objects.filter(
                question__label=label,
                value__in=["YES", "MAYBE"],
            ).values("report_id")
        )
```

Then add:

```python
from django.db.models import Count, QuerySet


def facet_label_counts(
    reports_qs: QuerySet, top_n: int = 20
) -> list[tuple[str, int]]:
    from radis.labels.models import Answer

    return list(
        Answer.objects.filter(report__in=reports_qs, value__in=["YES", "MAYBE"])
        .values("question__label")
        .annotate(c=Count("report", distinct=True))
        .order_by("-c", "question__label")[:top_n]
        .values_list("question__label", "c")
    )
```

- [ ] **Step 3: Test, commit**

```bash
uv run cli test -- radis/labels/tests/test_search_integration.py -q
git add radis/pgsearch/providers.py radis/labels/tests/test_search_integration.py
git commit -m "feat(labels): pgsearch labels filter + facet counts"
```

---

## Task 31: Surface facet counts in the search filters card

**Files:**
- Modify: `radis/search/views.py`, `radis/search/templates/search/search.html` (or `_search_results.html`)

- [ ] **Step 1: Add `label_facets` to context**

In `SearchView.get_context_data` (in `radis/search/views.py`):

```python
def get_context_data(self, **kwargs):
    ctx = super().get_context_data(**kwargs)
    from radis.pgsearch.providers import facet_label_counts

    # Use whichever attribute holds the current results — match existing patterns.
    result_qs = getattr(self, "result_queryset", None)
    if result_qs is not None:
        ctx["label_facets"] = facet_label_counts(result_qs, top_n=20)
    return ctx
```

- [ ] **Step 2: Render counts in the labels checkbox group**

In the existing filters card (`radis/search/templates/search/search.html` around the crispy form), append after the Filters card body:

```html
{% if label_facets %}
  <hr>
  <h6 class="card-subtitle text-muted">Labels</h6>
  <ul class="list-unstyled mb-0">
    {% for label, count in label_facets %}
      <li>
        <label>
          <input type="checkbox" name="labels" value="{{ label }}"
                 {% if label in form.cleaned_data.labels %}checked{% endif %}>
          {{ label }} <span class="text-muted">({{ count }})</span>
        </label>
      </li>
    {% endfor %}
  </ul>
{% endif %}
```

(If the SearchForm's crispy layout includes `labels` already, decide whether to render it through crispy or this manual block — keep one path.)

- [ ] **Step 3: Smoke test by hand**

```bash
uv run cli compose-up
```

Visit the search page in a browser, perform a search, and confirm the labels block shows up with counts (assumes at least one labeled report exists). Capture a screenshot or note the observation.

- [ ] **Step 4: Commit**

```bash
git add radis/search/
git commit -m "feat(labels): label facet counts on search results"
```

---

# Phase 8 — Operations, docs, acceptance

## Task 32: `radis.labels` logger config

**Files:**
- Modify: `radis/settings/base.py`

- [ ] **Step 1: Find LOGGING**

Grep for `LOGGING = {` in `radis/settings/base.py`. Inside `LOGGING["loggers"]`, add:

```python
"radis.labels": {
    "handlers": ["console"],  # match neighbouring loggers' handler keys
    "level": "INFO",
    "propagate": False,
},
```

- [ ] **Step 2: Verify**

```bash
uv run cli shell -c "import logging; logging.getLogger('radis.labels').info('hello'); print('ok')"
```

Expected: 'hello' appears in stdout; "ok" follows.

- [ ] **Step 3: Commit**

```bash
git add radis/settings/base.py
git commit -m "feat(labels): configure radis.labels logger"
```

---

## Task 33: `labels_status` management command + CLI wrapper

**Files:**
- Create: `radis/labels/management/commands/labels_status.py`
- Create: `radis/labels/tests/test_management.py`
- Modify: `cli.py`

- [ ] **Step 1: Failing tests**

Create `radis/labels/tests/test_management.py`:

```python
from io import StringIO

from django.core.management import call_command

from radis.reports.factories import ReportFactory
from radis.labels.factories import AnswerFactory, QuestionFactory


def test_labels_status_prints_coverage():
    ReportFactory()
    q = QuestionFactory(label="pneumonia", active=True)
    AnswerFactory(question=q, value="YES")
    buf = StringIO()
    call_command("labels_status", stdout=buf)
    out = buf.getvalue()
    assert "pneumonia" in out
    assert "total" in out.lower()
```

- [ ] **Step 2: Implement**

Create `radis/labels/management/commands/labels_status.py`:

```python
from django.core.management.base import BaseCommand
from django.db.models import Count, F, Q

from radis.reports.models import Report
from radis.labels.models import Answer, Question


class Command(BaseCommand):
    help = "Print labeling coverage for the corpus."

    def handle(self, *args, **opts):
        total = Report.objects.count()
        active_q = Question.objects.filter(active=True).count()
        self.stdout.write(f"Total reports: {total}")
        self.stdout.write(f"Active questions: {active_q}")
        if active_q == 0:
            self.stdout.write("No active questions — nothing to report.")
            return

        fully_current = (
            Report.objects.annotate(
                non_stale_count=Count(
                    "answers",
                    filter=Q(
                        answers__question__active=True,
                        answers__generated_at__gte=F(
                            "answers__question__updated_at"
                        ),
                    ),
                )
            )
            .filter(non_stale_count=active_q)
            .count()
        )
        self.stdout.write(f"Fully current: {fully_current}")
        self.stdout.write(f"Missing or stale: {total - fully_current}")

        for q in Question.objects.filter(active=True).order_by("group", "label"):
            counts = q.answers.aggregate(
                yes=Count("pk", filter=Q(value="YES")),
                no=Count("pk", filter=Q(value="NO")),
                maybe=Count("pk", filter=Q(value="MAYBE")),
                stale=Count("pk", filter=Q(generated_at__lt=q.updated_at)),
            )
            self.stdout.write(
                f"  [{q.group}] {q.label}: {counts['yes']} Y · "
                f"{counts['maybe']} M · {counts['no']} N · {counts['stale']} stale"
            )
```

- [ ] **Step 3: Wire into cli.py**

In `cli.py`, near the other local `@app.command()` functions (look at the existing `compose_up` / `compose_down` for the pattern), add:

```python
@app.command(name="labels-status")
def labels_status():
    """Print labeling coverage for the corpus."""
    import subprocess

    subprocess.run(["./manage.py", "labels_status"], check=True)
```

(Or follow whatever pattern an existing command uses to invoke `manage.py`.)

- [ ] **Step 4: Test, smoke, commit**

```bash
uv run cli test -- radis/labels/tests/test_management.py -q
uv run cli labels-status
git add radis/labels/management/ radis/labels/tests/test_management.py cli.py
git commit -m "feat(labels): labels_status management command + cli wrapper"
```

---

## Task 34: Real-LLM acceptance smoke test

**Files:**
- Create: `radis/labels/tests/test_acceptance.py`

- [ ] **Step 1: Write the test**

Create `radis/labels/tests/test_acceptance.py`:

```python
import time

import pytest

from radis.reports.factories import ReportFactory
from radis.labels.factories import QuestionFactory
from radis.labels.models import Answer


@pytest.mark.acceptance
def test_ingest_path_labels_a_report_end_to_end():
    q = QuestionFactory(
        label="lungs_clear",
        text="Are the lungs clear?",
        group="lung",
        active=True,
    )
    report = ReportFactory(body="No abnormalities, lungs clear.")

    deadline = time.time() + 60
    while time.time() < deadline:
        a = Answer.objects.filter(report=report, question=q).first()
        if a is not None:
            break
        time.sleep(1.0)
    else:
        pytest.fail("No Answer appeared within 60s")
    assert a.value == "YES"
```

- [ ] **Step 2: Run against the local LLM**

```bash
uv run cli compose-up
uv run cli test -- -m acceptance radis/labels/tests/test_acceptance.py -v
```

Expected: passes. Requires the LLM service and Procrastinate workers to be running.

- [ ] **Step 3: Commit**

```bash
git add radis/labels/tests/test_acceptance.py
git commit -m "test(labels): real-LLM acceptance smoke test"
```

---

## Task 35: Documentation updates

**Files:**
- Modify: `CLAUDE.md`, `KNOWLEDGE.md`

- [ ] **Step 1: Update `CLAUDE.md`**

After the `radis.extractions` bullet in the Django Apps list, add:

```markdown
- **radis.labels/**: Auto-labeling system. Admin-defined YES/NO/MAYBE questions are evaluated against report bodies by an LLM. Two paths share `label_report`: an ingest batch task on Report create/update, and an admin-triggered singleton backfill (`LabelingJob`/`LabelingTask`).
```

Append to Environment Variables:

```markdown
- `LABELING_INGEST_PRIORITY`, `LABELING_BACKFILL_PRIORITY`: Procrastinate priorities for the two paths.
- `LABELING_TASK_BATCH_SIZE`: Reports per task (default 100).
- `LABELING_LLM_CONCURRENCY_LIMIT`: Concurrent LLM calls per task (default 6).
- `LABELING_SYSTEM_PROMPT`: Override the default labeling prompt template.
```

Append to Troubleshooting:

```markdown
### Labels not appearing

- Check `uv run cli labels-status` for coverage.
- Verify `llm_worker` is running: `docker compose logs llm_worker | grep "radis.labels"`.
- Confirm `radis.labels.apps.LabelsConfig` is in `INSTALLED_APPS`.

### Backfill stuck in PREPARING

- `process_labeling_job` runs on the `default` queue. Verify `default_worker` is up.
- Look at `LabelingJobAdmin` change form for the live tasks-by-status counts.
```

- [ ] **Step 2: Update `KNOWLEDGE.md`**

Append:

```markdown
## Labeling Prompt Design

Questions are batched by their `group` string — all questions sharing a group go to the LLM in a single prompt. Answer space is fixed at YES/NO/MAYBE and enforced by the Pydantic schema, not just the prose. MAYBE is reserved for genuine ambiguity. Questions should be answerable from the report body alone.

Every report upload triggers re-labeling (the ETL signal is "I touched this; re-evaluate"). The per-group idempotency check inside `label_report` makes subsequent backfills cheap when only a subset of questions has changed.
```

- [ ] **Step 3: Commit**

```bash
git add CLAUDE.md KNOWLEDGE.md
git commit -m "docs(labels): document the auto-labeling feature"
```

---

## Task 36: Full-suite verification

**Files:** none

- [ ] **Step 1: Run the full labels suite**

```bash
uv run cli test -- radis/labels/ -q --tb=short
```

Expected: all green (excluding `-m acceptance` which was run separately in Task 34).

- [ ] **Step 2: Run the broader suite for regressions**

```bash
uv run cli test -- -q --tb=short
```

Expected: no regressions in `reports`, `search`, `pgsearch`, `core`.

- [ ] **Step 3: Run linter and formatter**

```bash
uv run cli lint
uv run cli format-code
```

Fix anything reported.

- [ ] **Step 4: (If anything changed in Step 3) commit cleanup**

```bash
git add -A
git diff --cached
git commit -m "chore(labels): post-implementation lint/format pass"
```

- [ ] **Step 5: Push and open PR**

```bash
git push -u origin feature/auto-labeling-design-kai
gh pr create --title "feat: auto-labeling feature" --body "$(cat <<'EOF'
## Summary
- New radis.labels app: admin-managed YES/NO/MAYBE questions classify reports via LLM
- Ingest batch labeling on Report create/update (chunked tasks at ingest priority)
- Singleton admin-triggered backfill (LabelingJob/LabelingTask), restart-safe
- Per-group idempotency to keep subsequent backfills cheap
- Label badges on report detail; label multi-select + facet counts in search

See `docs/superpowers/specs/2026-05-21-auto-labeling-design.md` for the design and `docs/superpowers/plans/2026-05-22-auto-labeling.md` for the plan.

## Test plan
- [ ] Unit + integration tests pass: `uv run cli test -- radis/labels/`
- [ ] Real-LLM acceptance smoke passes: `uv run cli test -- -m acceptance radis/labels/tests/test_acceptance.py`
- [ ] No regressions in reports/search/pgsearch
- [ ] Manual: create a Question via admin, ingest a Report via API, see the label appear on the report detail page
- [ ] Manual: click Run backfill in admin, observe PREPARING → IN_PROGRESS in the banner; click Cancel and confirm CANCELING then CANCELED

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

---

# Self-review checklist

**Spec coverage** — every section / requirement maps to a task:

- **Data model** — Tasks 2, 3, 4, 5
- **Ingest path** — Tasks 12 (`label_report`), 13 (`label_reports_in_parallel`), 14 (`label_report_batch` task), 15 (handler + chunking)
- **Backfill path** — Tasks 16 (scope), 17 (`create_labeling_tasks_streaming` with cancel check), 18 (processor with WARNING mapping), 19 (task), 20 (orchestrator + restart-safety delete + `delay`), 21 (`enqueue_all_pending_tasks`)
- **Per-group idempotency** — Task 11 (`_group_answers_are_current`); folded into `label_report` in Task 12
- **Admin UX** — Tasks 22 (Question), 23 (Answer), 24 (inline), 25 (LabelingJob list), 26 (Run/Cancel + banner template)
- **Report detail surfacing** — Tasks 27 (Cotton component), 28 (template + prefetch)
- **Search multi-select + facets** — Tasks 29 (SearchFilters + form), 30 (pgsearch translator + facet counts), 31 (template surfacing)
- **Settings + ops** — Tasks 6 (settings + prompt), 32 (logger), 33 (status command), 34 (acceptance smoke)
- **Documentation** — Task 35

**Type / name consistency** — checked across tasks:

- `Question`, `Answer`, `LabelingJob`, `LabelingTask` model names
- `label_report`, `label_reports_in_parallel`, `label_report_batch`, `process_labeling_task`, `process_labeling_job`, `enqueue_all_pending_tasks`, `create_labeling_tasks_streaming`, `find_reports_needing_work`, `_group_answers_are_current`, `upsert_answers`, `group_active_questions_by_group`, `sanitize_label`, `build_yes_no_maybe_schema`, `render_questions_prompt`
- Settings: `LABELING_INGEST_PRIORITY`, `LABELING_BACKFILL_PRIORITY`, `LABELING_TASK_BATCH_SIZE`, `LABELING_LLM_CONCURRENCY_LIMIT`, `LABELING_SYSTEM_PROMPT`, `DEFAULT_LABELING_SYSTEM_PROMPT`
- URL names: `labels_run_backfill`, `labels_cancel_backfill`
- Admin classes: `QuestionAdmin`, `AnswerAdmin`, `LabelingJobAdmin`, `AnswerInline`
- Custom admin view names: `run_backfill_view`, `cancel_backfill_view`
- Template paths: `templates/cotton/report_labels.html`, `templates/labels/admin/labelingjob_changelist.html`

**Verifications the implementer must do** (not failures; just reality-checks):

1. The exact short codes stored by `AnalysisJob.Status` (used in the RunSQL of Task 5).
2. The Procrastinate `app.configure_task("dotted.path", ...).defer(...)` pattern matches the project's existing usage in `extractions/tasks.py` and `subscriptions/tasks.py`.
3. `AnalysisTaskProcessor.start()` does NOT override `task.status` after `process_task` returns (Task 18 caveat). If it does, store the result on `task.message` and rework accordingly.
4. The path `report__id__in=` vs `id__in=` in `_build_filter_query` depends on whether the function targets `Report` or `ReportSearchVector`. Match neighbouring blocks (Task 30).
5. The Cotton component file naming convention (`report_labels.html` ↔ `<c-report-labels />`). Verify against an existing component (Task 27).
6. The `SearchFilters` / `SearchForm` exact module paths (grep before editing, Task 29).
7. The `UserFactory` import path for `LabelingJobFactory` (Task 4 — likely `adit_radis_shared.accounts.factories.UserFactory`).
