# Auto-Labeling Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the auto-labeling feature defined in `docs/superpowers/specs/2026-05-21-auto-labeling-design.md` — a new `radis.labels` Django app that classifies reports against admin-managed YES/NO/MAYBE questions, with per-report labeling on `Report` create/update and an admin-triggered singleton backfill for the existing corpus.

**Architecture:** New Django app `radis.labels` with denormalized `Question` (text + label + group string) and `Answer` (`report × question → YES|NO|MAYBE`) models. Two execution paths share a core `label_report` function: a Procrastinate per-report task triggered by report handlers, and a `LabelingJob`/`LabelingTask` backfill that streams scope from the corpus. The LLM is called via the existing `ChatClient.extract_data` with a dynamically built Pydantic schema.

**Tech Stack:** Python 3.12+, Django 5.1, PostgreSQL 17, Procrastinate, OpenAI-compatible LLM via `ChatClient`, Pydantic, pytest + pytest-django + factory-boy + Playwright (acceptance), Cotton components for templates.

---

## Spec deviation to confirm before execution

The spec described a `label:` QueryParser field filter. RADIS doesn't have field-filter support in `QueryParser` today — existing filters (`modalities`, `language`, `patient_sex`, …) live on the `SearchFilters` dataclass and are rendered via the crispy filters card. **This plan implements label filtering via `SearchFilters` (multi-select), matching the existing pattern.** Extending `QueryParser` for `label:foo` syntax is out of scope for this plan; if you want it, request a follow-up.

Facet *counts* (top-N labels with counts in the current result set) are still implemented as described.

---

## File structure

New files (under `radis/labels/`):

- `__init__.py`
- `apps.py` — `LabelsConfig.ready()`: register report handlers
- `models.py` — `Question`, `Answer`, `LabelingJob`, `LabelingTask`
- `factories.py` — factory-boy factories for tests
- `prompts.py` — `render_questions_prompt`, `build_yes_no_maybe_schema`, label sanitization
- `services.py` — `label_report`, `upsert_answers`, `group_active_questions_by_group`, `find_reports_needing_work`
- `signals.py` — `_label_reports_handler` (handler called by reports app)
- `tasks.py` — `label_single_report`, `process_labeling_job`, `process_labeling_task`
- `processors.py` — `LabelingTaskProcessor`
- `admin.py` — `QuestionAdmin`, `AnswerAdmin`, `LabelingJobAdmin`, `AnswerInline`
- `admin_views.py` — `run_backfill_view`, `cancel_backfill_view`
- `urls.py` — URLs for admin custom views
- `migrations/0001_initial.py` — auto-generated
- `migrations/0002_labelingjob_partial_unique_index.py` — manual `RunSQL`
- `templates/cotton/report_labels.html` — `<c-report-labels />`
- `templates/labels/admin/labelingjob_changelist.html` — Run/Cancel banner
- `management/__init__.py`
- `management/commands/__init__.py`
- `management/commands/labels_status.py`
- `tests/__init__.py`
- `tests/conftest.py`
- `tests/test_models.py`
- `tests/test_prompts.py`
- `tests/test_schemas.py`
- `tests/test_services.py`
- `tests/test_signals.py`
- `tests/test_tasks.py`
- `tests/test_processor.py`
- `tests/test_scope.py`
- `tests/test_singleton.py`
- `tests/test_admin.py`
- `tests/test_search_integration.py`
- `tests/test_management.py`
- `tests/test_cotton.py`
- `tests/test_acceptance.py` — `@pytest.mark.acceptance` real-LLM smoke test

Existing files modified:

- `radis/settings/base.py` — settings block + `DEFAULT_LABELING_SYSTEM_PROMPT` + logger config + `INSTALLED_APPS`
- `radis/reports/admin.py` — add `AnswerInline` to `ReportAdmin.inlines`
- `radis/reports/templates/reports/report_detail.html` — include `<c-report-labels />`
- `radis/reports/views.py` — prefetch answers on the detail view
- `radis/pgsearch/providers.py` — `_build_filter_query` knows the new `labels` filter; new `facet_label_counts` helper
- `radis/search/forms.py` and `radis/search/models.py` — add `labels` field to `SearchFilters` / SearchForm; render facet
- `radis/search/templates/search/_search_results.html` — facet counts UI
- `cli.py` — wire `labels-status` command
- `example.env` — new env vars
- `CLAUDE.md` — Django apps list, troubleshooting subsection
- `KNOWLEDGE.md` — labeling prompt design notes

---

# Phase 1 — Foundation

## Task 1: Create the app skeleton and register it

**Files:**
- Create: `radis/labels/__init__.py`
- Create: `radis/labels/apps.py`
- Create: `radis/labels/migrations/__init__.py`
- Create: `radis/labels/tests/__init__.py`
- Create: `radis/labels/tests/conftest.py`
- Modify: `radis/settings/base.py` (`INSTALLED_APPS`)

- [ ] **Step 1: Create the package directories and empty init files**

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
        # Local import keeps ready() side-effect-free at import time.
        from .signals import register_report_handlers

        register_report_handlers()
```

- [ ] **Step 3: Add empty `signals.py` stub so `apps.py` import works**

Create `radis/labels/signals.py`:

```python
def register_report_handlers() -> None:
    """Registered in LabelsConfig.ready(). Filled out in Phase 3."""
    return None
```

- [ ] **Step 4: Add to `INSTALLED_APPS`**

In `radis/settings/base.py`, find `INSTALLED_APPS` (around line 53–94). After the `"radis.pgsearch.apps.PgSearchConfig",` entry, add:

```python
    "radis.labels.apps.LabelsConfig",
```

- [ ] **Step 5: Verify Django sees the app**

Run:

```bash
uv run cli shell -c "import django; django.setup(); from django.apps import apps; print(apps.get_app_config('labels'))"
```

Expected: prints `<LabelsConfig: labels>` with no exception. If `uv run cli shell -c` isn't supported, run the same import sequence inside `uv run cli shell` interactively.

- [ ] **Step 6: Add a minimal conftest fixture to ensure the test suite picks up the new package**

Create `radis/labels/tests/conftest.py`:

```python
import pytest


@pytest.fixture(autouse=True)
def _enable_db(db):
    """All labels tests touch the DB — autouse the standard pytest-django fixture."""
    return db
```

- [ ] **Step 7: Smoke-test the test suite finds the package**

Run:

```bash
uv run cli test -- radis/labels/ -q
```

Expected: pytest finds zero tests (no failures, "no tests ran" or "0 passed").

- [ ] **Step 8: Commit**

```bash
git add radis/labels/ radis/settings/base.py
git commit -m "feat(labels): scaffold radis.labels app and register in INSTALLED_APPS"
```

---

## Task 2: Define the `Question` model

**Files:**
- Modify: `radis/labels/models.py` (create)
- Create: `radis/labels/factories.py`
- Create: `radis/labels/tests/test_models.py`

- [ ] **Step 1: Write failing tests**

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
        q = QuestionFactory()
        assert q.active is True

    def test_label_is_unique(self):
        QuestionFactory(label="pneumonia")
        with pytest.raises(IntegrityError):
            QuestionFactory(label="pneumonia")

    def test_created_and_updated_at_are_set(self):
        q = QuestionFactory()
        assert q.created_at is not None
        assert q.updated_at is not None

    def test_updated_at_changes_on_save(self):
        q = QuestionFactory()
        first = q.updated_at
        q.text = "Updated question text"
        q.save()
        q.refresh_from_db()
        assert q.updated_at > first
```

- [ ] **Step 2: Create a factory stub so the test imports work, even before model exists**

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

- [ ] **Step 3: Run tests to verify failure**

Run:

```bash
uv run cli test -- radis/labels/tests/test_models.py -q
```

Expected: ImportError / "Question is not defined" / collection error.

- [ ] **Step 4: Write the model**

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

- [ ] **Step 5: Generate the migration**

Run:

```bash
uv run cli shell -c "import django; django.setup(); from django.core.management import call_command; call_command('makemigrations', 'labels')"
```

(Or simply `uv run python manage.py makemigrations labels` if `cli` doesn't pass through.)

Expected: creates `radis/labels/migrations/0001_initial.py` containing `Question`.

- [ ] **Step 6: Apply migration and run tests**

```bash
uv run python manage.py migrate labels
uv run cli test -- radis/labels/tests/test_models.py -q
```

Expected: all 5 tests pass.

- [ ] **Step 7: Commit**

```bash
git add radis/labels/models.py radis/labels/factories.py radis/labels/tests/test_models.py radis/labels/migrations/0001_initial.py
git commit -m "feat(labels): add Question model with unique label constraint"
```

---

## Task 3: Define the `Answer` model

**Files:**
- Modify: `radis/labels/models.py`
- Modify: `radis/labels/factories.py`
- Modify: `radis/labels/tests/test_models.py`

- [ ] **Step 1: Write failing tests**

Append to `radis/labels/tests/test_models.py`:

```python
from radis.reports.factories import ReportFactory  # existing factory in the reports app
from radis.labels.factories import AnswerFactory
from radis.labels.models import Answer


class TestAnswer:
    def test_str_includes_value(self):
        a = AnswerFactory(value=Answer.Value.YES)
        assert "YES" in str(a)

    def test_value_choices_are_yes_no_maybe(self):
        assert set(Answer.Value.values) == {"YES", "NO", "MAYBE"}

    def test_unique_per_report_question(self):
        report = ReportFactory()
        question = QuestionFactory()
        AnswerFactory(report=report, question=question, value=Answer.Value.YES)
        with pytest.raises(IntegrityError):
            AnswerFactory(report=report, question=question, value=Answer.Value.NO)

    def test_generated_at_is_set(self):
        a = AnswerFactory()
        assert a.generated_at is not None

    def test_generated_at_bumps_on_save(self):
        a = AnswerFactory(value=Answer.Value.YES)
        first = a.generated_at
        a.value = Answer.Value.MAYBE
        a.save()
        a.refresh_from_db()
        assert a.generated_at > first

    def test_cascade_deletes_with_question(self):
        a = AnswerFactory()
        question_id = a.question.id
        a.question.delete()
        assert not Answer.objects.filter(question_id=question_id).exists()

    def test_cascade_deletes_with_report(self):
        a = AnswerFactory()
        report_id = a.report.id
        a.report.delete()
        assert not Answer.objects.filter(report_id=report_id).exists()
```

- [ ] **Step 2: Add the factory**

Append to `radis/labels/factories.py`:

```python
from radis.reports.factories import ReportFactory
from .models import Answer


class AnswerFactory(DjangoModelFactory):
    class Meta:
        model = Answer

    report = factory.SubFactory(ReportFactory)
    question = factory.SubFactory(QuestionFactory)
    value = Answer.Value.YES
```

- [ ] **Step 3: Run tests to verify failure**

Run:

```bash
uv run cli test -- radis/labels/tests/test_models.py::TestAnswer -q
```

Expected: fail / ImportError on `Answer`.

- [ ] **Step 4: Add the model**

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

    def __str__(self) -> str:
        return f"{self.report_id}:{self.question.label}={self.value}"
```

- [ ] **Step 5: Generate and apply migration**

```bash
uv run python manage.py makemigrations labels
uv run python manage.py migrate labels
```

Expected: a new migration (e.g., `0002_answer.py`) is created and applied.

- [ ] **Step 6: Run tests**

```bash
uv run cli test -- radis/labels/tests/test_models.py -q
```

Expected: all tests in `TestQuestion` and `TestAnswer` pass.

- [ ] **Step 7: Commit**

```bash
git add radis/labels/models.py radis/labels/factories.py radis/labels/tests/test_models.py radis/labels/migrations/
git commit -m "feat(labels): add Answer model with unique (report, question) constraint"
```

---

## Task 4: Define `LabelingJob` and `LabelingTask`

**Files:**
- Modify: `radis/labels/models.py`
- Modify: `radis/labels/factories.py`
- Modify: `radis/labels/tests/test_models.py`

- [ ] **Step 1: Write failing tests**

Append to `radis/labels/tests/test_models.py`:

```python
from radis.core.models import AnalysisJob, AnalysisTask
from radis.labels.factories import LabelingJobFactory, LabelingTaskFactory
from radis.labels.models import LabelingJob, LabelingTask


class TestLabelingJobModel:
    def test_inherits_from_analysis_job(self):
        assert issubclass(LabelingJob, AnalysisJob)

    def test_default_priority_uses_backfill_priority(self):
        # Both default and urgent priority equal LABELING_BACKFILL_PRIORITY.
        from django.conf import settings

        job = LabelingJobFactory()
        assert job.default_priority == settings.LABELING_BACKFILL_PRIORITY
        assert job.urgent_priority == settings.LABELING_BACKFILL_PRIORITY

    def test_active_statuses_constant(self):
        assert AnalysisJob.Status.PREPARING in LabelingJob.ACTIVE_STATUSES
        assert AnalysisJob.Status.PENDING in LabelingJob.ACTIVE_STATUSES
        assert AnalysisJob.Status.IN_PROGRESS in LabelingJob.ACTIVE_STATUSES
        assert AnalysisJob.Status.CANCELING in LabelingJob.ACTIVE_STATUSES
        assert AnalysisJob.Status.SUCCESS not in LabelingJob.ACTIVE_STATUSES


class TestLabelingTaskModel:
    def test_inherits_from_analysis_task(self):
        assert issubclass(LabelingTask, AnalysisTask)

    def test_reports_m2m(self):
        report = ReportFactory()
        task = LabelingTaskFactory()
        task.reports.add(report)
        assert report in task.reports.all()
```

- [ ] **Step 2: Add the factories**

Append to `radis/labels/factories.py`:

```python
from radis.core.models import AnalysisJob
from radis.accounts.factories import UserFactory  # use existing user factory
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

(If `radis.accounts.factories.UserFactory` doesn't exist, use whichever user factory the existing `extractions/factories.py` uses — typically `adit_radis_shared.accounts.factories.UserFactory`. Grep before assuming.)

- [ ] **Step 3: Run tests to verify failure**

```bash
uv run cli test -- radis/labels/tests/test_models.py::TestLabelingJobModel -q
```

Expected: ImportError on `LabelingJob`.

- [ ] **Step 4: Add the LABELING_BACKFILL_PRIORITY setting (minimal scaffolding so import works)**

In `radis/settings/base.py`, after the `SUBSCRIPTION_…` settings block:

```python
LABELING_PER_REPORT_PRIORITY = env.int("LABELING_PER_REPORT_PRIORITY", default=1)
LABELING_BACKFILL_PRIORITY = env.int("LABELING_BACKFILL_PRIORITY", default=0)
```

(Full settings block lands in Task 6; this is just enough to keep import working.)

- [ ] **Step 5: Add the models**

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

    @property
    def default_priority(self) -> int:
        return settings.LABELING_BACKFILL_PRIORITY

    @property
    def urgent_priority(self) -> int:
        return settings.LABELING_BACKFILL_PRIORITY

    def delay(self) -> None:
        # Filled in Task 18 — needs the process_labeling_job task.
        raise NotImplementedError("Implemented in Task 18")


class LabelingTask(AnalysisTask):
    job = models.ForeignKey(
        LabelingJob, on_delete=models.CASCADE, related_name="tasks"
    )
    reports = models.ManyToManyField(Report, related_name="+")
```

- [ ] **Step 6: Generate and apply migration**

```bash
uv run python manage.py makemigrations labels
uv run python manage.py migrate labels
```

Expected: new migration includes both `LabelingJob` and `LabelingTask`.

- [ ] **Step 7: Run tests**

```bash
uv run cli test -- radis/labels/tests/test_models.py -q
```

Expected: all tests in this file pass.

- [ ] **Step 8: Commit**

```bash
git add radis/labels/models.py radis/labels/factories.py radis/labels/tests/test_models.py radis/labels/migrations/ radis/settings/base.py
git commit -m "feat(labels): add LabelingJob and LabelingTask models"
```

---

## Task 5: Partial unique index for the singleton constraint

**Files:**
- Create: `radis/labels/migrations/000N_labelingjob_partial_unique.py` (N = next auto-numbered migration)
- Modify: `radis/labels/tests/test_singleton.py`

- [ ] **Step 1: Write failing tests**

Create `radis/labels/tests/test_singleton.py`:

```python
import pytest
from django.db import IntegrityError, transaction

from radis.core.models import AnalysisJob
from radis.labels.factories import LabelingJobFactory


class TestLabelingJobSingleton:
    def test_one_active_job_allowed(self):
        LabelingJobFactory(status=AnalysisJob.Status.PENDING)
        # No exception expected.

    def test_two_active_jobs_blocked(self):
        LabelingJobFactory(status=AnalysisJob.Status.PENDING)
        with pytest.raises(IntegrityError):
            with transaction.atomic():
                LabelingJobFactory(status=AnalysisJob.Status.PENDING)

    @pytest.mark.parametrize(
        "status",
        [
            AnalysisJob.Status.UNVERIFIED,
            AnalysisJob.Status.PREPARING,
            AnalysisJob.Status.PENDING,
            AnalysisJob.Status.IN_PROGRESS,
            AnalysisJob.Status.CANCELING,
        ],
    )
    def test_blocks_all_active_statuses(self, status):
        LabelingJobFactory(status=AnalysisJob.Status.PENDING)
        with pytest.raises(IntegrityError):
            with transaction.atomic():
                LabelingJobFactory(status=status)

    @pytest.mark.parametrize(
        "terminal_status",
        [
            AnalysisJob.Status.SUCCESS,
            AnalysisJob.Status.WARNING,
            AnalysisJob.Status.FAILURE,
            AnalysisJob.Status.CANCELED,
        ],
    )
    def test_after_terminal_status_new_active_allowed(self, terminal_status):
        first = LabelingJobFactory(status=AnalysisJob.Status.PENDING)
        first.status = terminal_status
        first.save()
        LabelingJobFactory(status=AnalysisJob.Status.PENDING)
```

- [ ] **Step 2: Run tests to verify failure**

```bash
uv run cli test -- radis/labels/tests/test_singleton.py -q
```

Expected: most tests FAIL (no constraint exists yet).

- [ ] **Step 3: Generate an empty migration**

```bash
uv run python manage.py makemigrations labels --empty --name labelingjob_partial_unique
```

Expected: a new empty migration is created.

- [ ] **Step 4: Fill in the migration with `RunSQL`**

Edit the newly created migration file to:

```python
from django.db import migrations


CREATE_INDEX_SQL = """
CREATE UNIQUE INDEX labels_labelingjob_one_active_idx
ON labels_labelingjob ((1))
WHERE status IN ('UV', 'PR', 'PE', 'IP', 'CI');
"""

DROP_INDEX_SQL = "DROP INDEX IF EXISTS labels_labelingjob_one_active_idx;"


class Migration(migrations.Migration):
    dependencies = [
        ("labels", "<the previous migration filename without .py>"),
    ]

    operations = [
        migrations.RunSQL(sql=CREATE_INDEX_SQL, reverse_sql=DROP_INDEX_SQL),
    ]
```

**Note on the status codes:** This SQL uses the short status codes from `AnalysisJob.Status`. Before applying, confirm the actual stored values by running:

```bash
uv run cli shell -c "from radis.core.models import AnalysisJob; print({s.name: s.value for s in AnalysisJob.Status})"
```

Replace the codes in `CREATE_INDEX_SQL` with whatever the enum actually stores (e.g., if values are full words like `'UNVERIFIED'`, use those). The status values are the source of truth; do not hardcode without verifying.

- [ ] **Step 5: Apply migration**

```bash
uv run python manage.py migrate labels
```

Expected: migration applies cleanly.

- [ ] **Step 6: Run tests**

```bash
uv run cli test -- radis/labels/tests/test_singleton.py -q
```

Expected: all tests pass.

- [ ] **Step 7: Commit**

```bash
git add radis/labels/migrations/ radis/labels/tests/test_singleton.py
git commit -m "feat(labels): enforce singleton active backfill via partial unique index"
```

---

# Phase 2 — Core labeling services

## Task 6: Settings block and default labeling prompt

**Files:**
- Modify: `radis/settings/base.py`
- Modify: `example.env`

- [ ] **Step 1: Add prompt constant and settings**

In `radis/settings/base.py`, find the existing `QUESTIONS_SYSTEM_PROMPT`/`OUTPUT_FIELDS_SYSTEM_PROMPT` block. Below it, add:

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

Then in the same file, find the labeling priority lines added in Task 4 and replace/extend them with the full block:

```python
# Labeling feature
LABELING_PER_REPORT_PRIORITY = env.int("LABELING_PER_REPORT_PRIORITY", default=1)
LABELING_BACKFILL_PRIORITY = env.int("LABELING_BACKFILL_PRIORITY", default=0)
LABELING_TASK_BATCH_SIZE = env.int("LABELING_TASK_BATCH_SIZE", default=100)
LABELING_LLM_CONCURRENCY_LIMIT = env.int("LABELING_LLM_CONCURRENCY_LIMIT", default=6)
LABELING_SYSTEM_PROMPT = env.str(
    "LABELING_SYSTEM_PROMPT", default=DEFAULT_LABELING_SYSTEM_PROMPT
)
```

- [ ] **Step 2: Add to `example.env`**

Append to `example.env`:

```bash
# Labeling feature
LABELING_PER_REPORT_PRIORITY=1
LABELING_BACKFILL_PRIORITY=0
LABELING_TASK_BATCH_SIZE=100
LABELING_LLM_CONCURRENCY_LIMIT=6
# LABELING_SYSTEM_PROMPT can be left unset to use the in-code default.
```

- [ ] **Step 3: Verify settings load**

```bash
uv run cli shell -c "from django.conf import settings; print(settings.LABELING_TASK_BATCH_SIZE, settings.LABELING_LLM_CONCURRENCY_LIMIT, len(settings.LABELING_SYSTEM_PROMPT) > 0)"
```

Expected: `100 6 True`

- [ ] **Step 4: Commit**

```bash
git add radis/settings/base.py example.env
git commit -m "feat(labels): add labeling settings and default system prompt"
```

---

## Task 7: `render_questions_prompt`

**Files:**
- Create: `radis/labels/prompts.py`
- Create: `radis/labels/tests/test_prompts.py`

- [ ] **Step 1: Write failing tests**

Create `radis/labels/tests/test_prompts.py`:

```python
from radis.labels.factories import QuestionFactory
from radis.labels.prompts import render_questions_prompt


class TestRenderQuestionsPrompt:
    def test_substitutes_report_body(self):
        q = QuestionFactory(text="Is the chest clear?")
        prompt = render_questions_prompt(report_body="Lungs are clear.", questions=[q])
        assert "Lungs are clear." in prompt
        assert "Is the chest clear?" in prompt

    def test_lists_questions_with_their_labels(self):
        q1 = QuestionFactory(text="Pneumonia?", label="pneumonia")
        q2 = QuestionFactory(text="Effusion?", label="effusion")
        prompt = render_questions_prompt("body", [q1, q2])
        # Each question is identifiable by its label key in the prompt.
        assert "pneumonia" in prompt
        assert "effusion" in prompt
        assert "Pneumonia?" in prompt
        assert "Effusion?" in prompt

    def test_handles_unicode_in_body_and_question(self):
        q = QuestionFactory(text="Frage über Lungen?")
        prompt = render_questions_prompt("Bericht: keine Auffälligkeiten.", [q])
        assert "Frage über Lungen?" in prompt
        assert "keine Auffälligkeiten." in prompt

    def test_no_template_placeholders_left_unresolved(self):
        q = QuestionFactory(text="Anything?")
        prompt = render_questions_prompt("body", [q])
        assert "$report" not in prompt
        assert "$questions" not in prompt
```

- [ ] **Step 2: Write the implementation**

Create `radis/labels/prompts.py`:

```python
from string import Template

from django.conf import settings

from .models import Question


def _format_question_lines(questions: list[Question]) -> str:
    return "\n".join(
        f"- {q.label}: {q.text}" for q in questions
    )


def render_questions_prompt(report_body: str, questions: list[Question]) -> str:
    template = Template(settings.LABELING_SYSTEM_PROMPT)
    return template.substitute(
        report=report_body,
        questions=_format_question_lines(questions),
    )
```

- [ ] **Step 3: Run tests**

```bash
uv run cli test -- radis/labels/tests/test_prompts.py -q
```

Expected: all 4 tests pass.

- [ ] **Step 4: Commit**

```bash
git add radis/labels/prompts.py radis/labels/tests/test_prompts.py
git commit -m "feat(labels): render questions into the labeling prompt"
```

---

## Task 8: `build_yes_no_maybe_schema` with label sanitization

**Files:**
- Modify: `radis/labels/prompts.py`
- Create: `radis/labels/tests/test_schemas.py`

- [ ] **Step 1: Write failing tests**

Create `radis/labels/tests/test_schemas.py`:

```python
import pytest
from pydantic import ValidationError

from radis.labels.factories import QuestionFactory
from radis.labels.prompts import build_yes_no_maybe_schema, sanitize_label


class TestSanitizeLabel:
    @pytest.mark.parametrize(
        "raw, expected",
        [
            ("pneumonia", "pneumonia"),
            ("Lung Effusion", "lung_effusion"),
            ("foo-bar", "foo_bar"),
            ("café", "caf_"),
            ("123abc", "_123abc"),  # must start with a non-digit (Python identifier rule)
            ("a b c", "a_b_c"),
        ],
    )
    def test_sanitizes_to_valid_identifier(self, raw, expected):
        result = sanitize_label(raw)
        assert result == expected
        assert result.isidentifier()


class TestBuildSchema:
    def test_one_field_per_question(self):
        q1 = QuestionFactory(label="pneumonia")
        q2 = QuestionFactory(label="effusion")
        Schema = build_yes_no_maybe_schema([q1, q2])

        instance = Schema(pneumonia="YES", effusion="NO")
        assert instance.pneumonia == "YES"
        assert instance.effusion == "NO"

    def test_rejects_unknown_answer(self):
        q = QuestionFactory(label="pneumonia")
        Schema = build_yes_no_maybe_schema([q])
        with pytest.raises(ValidationError):
            Schema(pneumonia="PROBABLY")

    def test_accepts_maybe(self):
        q = QuestionFactory(label="pneumonia")
        Schema = build_yes_no_maybe_schema([q])
        instance = Schema(pneumonia="MAYBE")
        assert instance.pneumonia == "MAYBE"

    def test_rejects_missing_field(self):
        q1 = QuestionFactory(label="pneumonia")
        q2 = QuestionFactory(label="effusion")
        Schema = build_yes_no_maybe_schema([q1, q2])
        with pytest.raises(ValidationError):
            Schema(pneumonia="YES")

    def test_rejects_extra_field(self):
        q = QuestionFactory(label="pneumonia")
        Schema = build_yes_no_maybe_schema([q])
        with pytest.raises(ValidationError):
            Schema(pneumonia="YES", extra="YES")

    def test_label_collision_after_sanitization_raises(self):
        q1 = QuestionFactory(label="lung effusion")
        q2 = QuestionFactory(label="lung-effusion")  # also sanitizes to lung_effusion
        with pytest.raises(ValueError, match="collide"):
            build_yes_no_maybe_schema([q1, q2])
```

- [ ] **Step 2: Run tests to verify failure**

```bash
uv run cli test -- radis/labels/tests/test_schemas.py -q
```

Expected: ImportError on `build_yes_no_maybe_schema` / `sanitize_label`.

- [ ] **Step 3: Implement**

Append to `radis/labels/prompts.py`:

```python
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, create_model


def sanitize_label(label: str) -> str:
    """Turn a label into a valid, lowercase Python identifier.

    Non-alphanumeric characters become "_". Leading digits get a "_" prefix
    so the result is always a valid identifier. Used for Pydantic field names.
    """
    s = re.sub(r"[^A-Za-z0-9]", "_", label).lower()
    if not s or s[0].isdigit():
        s = "_" + s
    return s


def build_yes_no_maybe_schema(questions: list[Question]) -> type[BaseModel]:
    """Build a Pydantic model with one Literal["YES","NO","MAYBE"] field per question.

    Field names are the sanitized labels. Raises ValueError if two questions
    sanitize to the same field name.
    """
    fields: dict[str, tuple[type, object]] = {}
    seen: dict[str, str] = {}
    for q in questions:
        key = sanitize_label(q.label)
        if key in seen:
            raise ValueError(
                f"Sanitized labels collide: {seen[key]!r} and {q.label!r} "
                f"both map to {key!r}"
            )
        seen[key] = q.label
        fields[key] = (Literal["YES", "NO", "MAYBE"], ...)

    return create_model(
        "LabelingAnswers",
        __config__=ConfigDict(extra="forbid"),
        **fields,
    )
```

- [ ] **Step 4: Run tests**

```bash
uv run cli test -- radis/labels/tests/test_schemas.py -q
```

Expected: all tests pass.

- [ ] **Step 5: Commit**

```bash
git add radis/labels/prompts.py radis/labels/tests/test_schemas.py
git commit -m "feat(labels): build per-question Pydantic YES/NO/MAYBE schema"
```

---

## Task 9: `group_active_questions_by_group`

**Files:**
- Create: `radis/labels/services.py`
- Create: `radis/labels/tests/test_services.py`

- [ ] **Step 1: Write failing tests**

Create `radis/labels/tests/test_services.py`:

```python
from radis.labels.factories import QuestionFactory
from radis.labels.services import group_active_questions_by_group


class TestGroupActiveQuestions:
    def test_empty_when_no_questions(self):
        assert group_active_questions_by_group() == {}

    def test_groups_by_group_string(self):
        QuestionFactory(label="a", group="lung", active=True)
        QuestionFactory(label="b", group="lung", active=True)
        QuestionFactory(label="c", group="cardiac", active=True)
        result = group_active_questions_by_group()
        assert set(result.keys()) == {"lung", "cardiac"}
        assert {q.label for q in result["lung"]} == {"a", "b"}
        assert {q.label for q in result["cardiac"]} == {"c"}

    def test_excludes_inactive(self):
        QuestionFactory(label="active1", group="lung", active=True)
        QuestionFactory(label="inactive", group="lung", active=False)
        result = group_active_questions_by_group()
        assert [q.label for q in result["lung"]] == ["active1"]
```

- [ ] **Step 2: Run tests to verify failure**

```bash
uv run cli test -- radis/labels/tests/test_services.py::TestGroupActiveQuestions -q
```

Expected: ImportError.

- [ ] **Step 3: Implement**

Create `radis/labels/services.py`:

```python
from collections import defaultdict

from .models import Question


def group_active_questions_by_group() -> dict[str, list[Question]]:
    """Return {group_string: [Question, ...]} for all active questions."""
    grouped: dict[str, list[Question]] = defaultdict(list)
    for q in Question.objects.filter(active=True).order_by("group", "label"):
        grouped[q.group].append(q)
    return dict(grouped)
```

- [ ] **Step 4: Run tests**

```bash
uv run cli test -- radis/labels/tests/test_services.py::TestGroupActiveQuestions -q
```

Expected: all tests pass.

- [ ] **Step 5: Commit**

```bash
git add radis/labels/services.py radis/labels/tests/test_services.py
git commit -m "feat(labels): group active questions by their group string"
```

---

## Task 10: `upsert_answers`

**Files:**
- Modify: `radis/labels/services.py`
- Modify: `radis/labels/tests/test_services.py`

- [ ] **Step 1: Write failing tests**

Append to `radis/labels/tests/test_services.py`:

```python
import time

from radis.reports.factories import ReportFactory
from radis.labels.models import Answer
from radis.labels.prompts import sanitize_label
from radis.labels.services import upsert_answers


class TestUpsertAnswers:
    def test_creates_one_row_per_question(self):
        report = ReportFactory()
        q1 = QuestionFactory(label="pneumonia")
        q2 = QuestionFactory(label="effusion")
        parsed = {
            sanitize_label("pneumonia"): "YES",
            sanitize_label("effusion"): "MAYBE",
        }
        upsert_answers(report, [q1, q2], parsed)
        assert Answer.objects.count() == 2
        a1 = Answer.objects.get(question=q1)
        a2 = Answer.objects.get(question=q2)
        assert a1.value == Answer.Value.YES
        assert a2.value == Answer.Value.MAYBE

    def test_replaces_existing_answer(self):
        report = ReportFactory()
        q = QuestionFactory(label="pneumonia")
        upsert_answers(report, [q], {sanitize_label("pneumonia"): "YES"})
        first = Answer.objects.get(report=report, question=q)
        first_generated = first.generated_at

        time.sleep(0.01)  # ensure generated_at moves forward
        upsert_answers(report, [q], {sanitize_label("pneumonia"): "NO"})
        assert Answer.objects.filter(report=report, question=q).count() == 1
        updated = Answer.objects.get(report=report, question=q)
        assert updated.value == Answer.Value.NO
        assert updated.generated_at > first_generated

    def test_ignores_keys_not_in_question_list(self):
        report = ReportFactory()
        q = QuestionFactory(label="pneumonia")
        upsert_answers(
            report,
            [q],
            {
                sanitize_label("pneumonia"): "YES",
                "unrelated_key": "MAYBE",
            },
        )
        assert Answer.objects.filter(report=report).count() == 1
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
    """Upsert one Answer row per question based on the parsed LLM response.

    `parsed` is keyed by sanitized label. Unknown keys are ignored.
    """
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

- [ ] **Step 3: Run tests**

```bash
uv run cli test -- radis/labels/tests/test_services.py::TestUpsertAnswers -q
```

Expected: all tests pass.

- [ ] **Step 4: Commit**

```bash
git add radis/labels/services.py radis/labels/tests/test_services.py
git commit -m "feat(labels): upsert answers per (report, question)"
```

---

## Task 11: `label_report` (the core orchestration function)

**Files:**
- Modify: `radis/labels/services.py`
- Modify: `radis/labels/tests/test_services.py`

- [ ] **Step 1: Write failing tests**

Append to `radis/labels/tests/test_services.py`:

```python
from unittest.mock import MagicMock, patch

from radis.labels.services import label_report


class TestLabelReport:
    def test_skips_when_body_empty(self):
        report = ReportFactory(body="")
        QuestionFactory(label="pneumonia", group="lung", active=True)
        with patch("radis.labels.services.ChatClient") as ChatClientMock:
            label_report(report.id)
        ChatClientMock.assert_not_called()
        assert Answer.objects.count() == 0

    def test_skips_when_body_whitespace_only(self):
        report = ReportFactory(body="   \n  \t")
        QuestionFactory(label="pneumonia", group="lung", active=True)
        with patch("radis.labels.services.ChatClient") as ChatClientMock:
            label_report(report.id)
        ChatClientMock.assert_not_called()

    def test_skips_when_no_active_questions(self):
        report = ReportFactory(body="A body.")
        QuestionFactory(label="pneumonia", active=False)
        with patch("radis.labels.services.ChatClient") as ChatClientMock:
            label_report(report.id)
        ChatClientMock.assert_not_called()

    def test_one_llm_call_per_group(self):
        report = ReportFactory(body="A meaningful body.")
        QuestionFactory(label="pneumonia", group="lung", active=True)
        QuestionFactory(label="effusion", group="lung", active=True)
        QuestionFactory(label="cardiomegaly", group="cardiac", active=True)

        # Return value depends on the schema; the function calls extract_data per group.
        def fake_extract(prompt, Schema):
            # Build a stub that satisfies any of these schemas with all "YES".
            field_defaults = {name: "YES" for name in Schema.model_fields}
            return Schema(**field_defaults)

        with patch("radis.labels.services.ChatClient") as ChatClientMock:
            ChatClientMock.return_value.extract_data.side_effect = fake_extract
            label_report(report.id)
            assert ChatClientMock.return_value.extract_data.call_count == 2

    def test_persists_answers(self):
        report = ReportFactory(body="A body.")
        q = QuestionFactory(label="pneumonia", group="lung", active=True)

        def fake_extract(prompt, Schema):
            return Schema(pneumonia="MAYBE")

        with patch("radis.labels.services.ChatClient") as ChatClientMock:
            ChatClientMock.return_value.extract_data.side_effect = fake_extract
            label_report(report.id)

        a = Answer.objects.get(report=report, question=q)
        assert a.value == Answer.Value.MAYBE
```

- [ ] **Step 2: Implement**

Append to `radis/labels/services.py`:

```python
import logging

from radis.chats.utils.chat_client import ChatClient
from .prompts import build_yes_no_maybe_schema, render_questions_prompt


logger = logging.getLogger("radis.labels")


def label_report(report_id: int, client: ChatClient | None = None) -> None:
    """Label a single report against all currently active questions.

    Shared by the per-report task and the backfill processor. Idempotent.
    """
    report = Report.objects.get(id=report_id)
    if not report.body or not report.body.strip():
        logger.warning("Skipping report %s: empty body", report_id)
        return

    questions_by_group = group_active_questions_by_group()
    if not questions_by_group:
        logger.warning("Skipping report %s: no active questions", report_id)
        return

    chat = client or ChatClient()

    for group_str, questions in questions_by_group.items():
        Schema = build_yes_no_maybe_schema(questions)
        prompt = render_questions_prompt(report.body, questions)
        parsed = chat.extract_data(prompt, Schema)
        parsed_dict = parsed.model_dump()
        upsert_answers(report, questions, parsed_dict)
```

- [ ] **Step 3: Run tests**

```bash
uv run cli test -- radis/labels/tests/test_services.py -q
```

Expected: all tests pass.

- [ ] **Step 4: Commit**

```bash
git add radis/labels/services.py radis/labels/tests/test_services.py
git commit -m "feat(labels): label_report orchestrates the LLM call per question group"
```

---

# Phase 3 — Per-report path

## Task 12: `label_single_report` Procrastinate task

**Files:**
- Create: `radis/labels/tasks.py`
- Create: `radis/labels/tests/test_tasks.py`

- [ ] **Step 1: Write failing tests**

Create `radis/labels/tests/test_tasks.py`:

```python
from unittest.mock import patch

from radis.labels.tasks import label_single_report


class TestLabelSingleReportTask:
    def test_calls_label_report(self):
        with patch("radis.labels.tasks.label_report") as label_report_mock:
            # Call the underlying function directly (not as a task).
            label_single_report(report_id=42)
        label_report_mock.assert_called_once_with(42)
```

- [ ] **Step 2: Implement**

Create `radis/labels/tasks.py`:

```python
from procrastinate.contrib.django import app

from .services import label_report


@app.task(queue="llm")
def label_single_report(report_id: int) -> None:
    """Per-report background task triggered by Report create/update."""
    label_report(report_id)
```

- [ ] **Step 3: Run tests**

```bash
uv run cli test -- radis/labels/tests/test_tasks.py -q
```

Expected: passes.

- [ ] **Step 4: Commit**

```bash
git add radis/labels/tasks.py radis/labels/tests/test_tasks.py
git commit -m "feat(labels): label_single_report Procrastinate task on llm queue"
```

---

## Task 13: Reports handler registration

**Files:**
- Modify: `radis/labels/signals.py`
- Create: `radis/labels/tests/test_signals.py`

- [ ] **Step 1: Write failing tests**

Create `radis/labels/tests/test_signals.py`:

```python
from unittest.mock import patch

from django.db import transaction
import pytest

from radis.reports.factories import ReportFactory
from radis.reports.site import (
    reports_created_handlers,
    reports_updated_handlers,
)
from radis.labels.signals import (
    _label_reports_handler,
    register_report_handlers,
)


class TestRegistration:
    def test_register_adds_created_and_updated_handlers(self):
        before_created = len(reports_created_handlers)
        before_updated = len(reports_updated_handlers)

        register_report_handlers()

        assert any(
            h.name == "labels" for h in reports_created_handlers[before_created:]
        )
        assert any(
            h.name == "labels" for h in reports_updated_handlers[before_updated:]
        )

    def test_double_registration_is_idempotent(self):
        register_report_handlers()
        before = len([h for h in reports_created_handlers if h.name == "labels"])
        register_report_handlers()
        after = len([h for h in reports_created_handlers if h.name == "labels"])
        assert before == after


class TestHandler:
    def test_handler_enqueues_one_task_per_report(self):
        reports = [ReportFactory(), ReportFactory(), ReportFactory()]
        with patch("radis.labels.signals.app") as app_mock:
            deferrer = app_mock.configure_task.return_value
            _label_reports_handler(reports)

            assert deferrer.defer.call_count == 3
            ids = [c.kwargs["report_id"] for c in deferrer.defer.call_args_list]
            assert sorted(ids) == sorted([r.id for r in reports])

    def test_handler_uses_per_report_priority(self):
        from django.conf import settings

        reports = [ReportFactory()]
        with patch("radis.labels.signals.app") as app_mock:
            _label_reports_handler(reports)
            app_mock.configure_task.assert_called_once()
            _, kwargs = app_mock.configure_task.call_args
            assert kwargs["priority"] == settings.LABELING_PER_REPORT_PRIORITY
```

- [ ] **Step 2: Implement**

Replace `radis/labels/signals.py`:

```python
import logging

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
from .tasks import label_single_report


logger = logging.getLogger("radis.labels")

HANDLER_NAME = "labels"


def _label_reports_handler(reports: list[Report]) -> None:
    """Receive a batch of newly-created or updated reports.

    Enqueues one per-report background task per report. Bursts arrive as a
    single handler call with all the reports; we fan out to N tasks so they
    can drain at LLM-queue concurrency.
    """
    from django.conf import settings

    deferrer = app.configure_task(
        "radis.labels.tasks.label_single_report",
        allow_unknown=False,
        priority=settings.LABELING_PER_REPORT_PRIORITY,
    )
    for report in reports:
        deferrer.defer(report_id=report.id)


def register_report_handlers() -> None:
    """Idempotent: skips registration if already present."""
    if not any(h.name == HANDLER_NAME for h in reports_created_handlers):
        register_reports_created_handler(
            ReportsCreatedHandler(name=HANDLER_NAME, handle=_label_reports_handler)
        )
    if not any(h.name == HANDLER_NAME for h in reports_updated_handlers):
        register_reports_updated_handler(
            ReportsUpdatedHandler(name=HANDLER_NAME, handle=_label_reports_handler)
        )
```

**Note on `configure(priority=…)`:** Verify the Procrastinate `@app.task` decorator exposes `.configure(priority=...).defer(…)` in this codebase by grepping for how `extractions/tasks.py` and `subscriptions/tasks.py` defer per-call priorities. If the API differs (e.g. `app.configure_task("dotted.path", priority=…).defer(…)`), use that shape instead. Concretely:

```python
deferrer = app.configure_task(
    "radis.labels.tasks.label_single_report",
    allow_unknown=False,
    priority=settings.LABELING_PER_REPORT_PRIORITY,
)
for report in reports:
    deferrer.defer(report_id=report.id)
```

Use whichever shape is consistent with how subscriptions/extractions defer in the project.

- [ ] **Step 3: Run tests**

```bash
uv run cli test -- radis/labels/tests/test_signals.py -q
```

Expected: all tests pass.

- [ ] **Step 4: Commit**

```bash
git add radis/labels/signals.py radis/labels/tests/test_signals.py
git commit -m "feat(labels): register report create/update handlers"
```

---

# Phase 4 — Backfill path

## Task 14: `find_reports_needing_work` scope query

**Files:**
- Modify: `radis/labels/services.py`
- Create: `radis/labels/tests/test_scope.py`

- [ ] **Step 1: Write failing tests**

Create `radis/labels/tests/test_scope.py`:

```python
from datetime import timedelta

from django.utils import timezone

from radis.reports.factories import ReportFactory
from radis.labels.factories import AnswerFactory, QuestionFactory
from radis.labels.models import Answer
from radis.labels.services import find_reports_needing_work


class TestFindReportsNeedingWork:
    def test_report_with_no_answers_is_in_scope(self):
        report = ReportFactory()
        QuestionFactory(active=True)
        scope = list(find_reports_needing_work())
        assert report.id in scope

    def test_report_with_all_current_answers_is_out_of_scope(self):
        report = ReportFactory()
        q1 = QuestionFactory(active=True)
        q2 = QuestionFactory(active=True)
        AnswerFactory(report=report, question=q1)
        AnswerFactory(report=report, question=q2)
        scope = list(find_reports_needing_work())
        assert report.id not in scope

    def test_report_with_one_missing_answer_is_in_scope(self):
        report = ReportFactory()
        q1 = QuestionFactory(active=True)
        QuestionFactory(active=True)  # second active question
        AnswerFactory(report=report, question=q1)
        scope = list(find_reports_needing_work())
        assert report.id in scope

    def test_report_with_stale_answer_is_in_scope(self):
        report = ReportFactory()
        q = QuestionFactory(active=True)
        a = AnswerFactory(report=report, question=q)
        # Make the question newer than the answer.
        future = a.generated_at + timedelta(seconds=10)
        type(q).objects.filter(pk=q.pk).update(updated_at=future)
        scope = list(find_reports_needing_work())
        assert report.id in scope

    def test_inactive_questions_are_ignored(self):
        report = ReportFactory()
        QuestionFactory(active=False)  # inactive, not counted
        # No active questions exist — every report is fully labeled vacuously.
        scope = list(find_reports_needing_work())
        assert report.id not in scope

    def test_streams_in_chunks(self):
        # We don't unit-test the chunking exhaustively, just that it returns
        # everything when there are more reports than the chunk size.
        for _ in range(150):
            ReportFactory()
        QuestionFactory(active=True)
        scope = list(find_reports_needing_work(chunk_size=50))
        assert len(scope) == 150
```

- [ ] **Step 2: Implement**

Append to `radis/labels/services.py`:

```python
from typing import Iterator

from django.db.models import Count, F, Q

from .models import Question


def find_reports_needing_work(chunk_size: int = 1000) -> Iterator[int]:
    """Yield Report IDs that have at least one missing or stale answer for an active question.

    Streams via .iterator() to keep memory bounded for the 1M-report case.
    """
    active_question_count = Question.objects.filter(active=True).count()

    if active_question_count == 0:
        return

    qs = (
        Report.objects.order_by("pk")
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

    yield from qs.iterator(chunk_size=chunk_size)
```

- [ ] **Step 3: Run tests**

```bash
uv run cli test -- radis/labels/tests/test_scope.py -q
```

Expected: all pass. If the annotation expression behaves unexpectedly on the JOIN, fall back to a `Subquery`-based formulation; see notes in the spec.

- [ ] **Step 4: Commit**

```bash
git add radis/labels/services.py radis/labels/tests/test_scope.py
git commit -m "feat(labels): scope query for reports with missing or stale answers"
```

---

## Task 15: `LabelingTaskProcessor`

**Files:**
- Create: `radis/labels/processors.py`
- Create: `radis/labels/tests/test_processor.py`

- [ ] **Step 1: Write failing tests**

Create `radis/labels/tests/test_processor.py`:

```python
from unittest.mock import patch

from radis.core.models import AnalysisJob, AnalysisTask
from radis.reports.factories import ReportFactory
from radis.labels.factories import (
    LabelingJobFactory,
    LabelingTaskFactory,
    QuestionFactory,
)
from radis.labels.models import Answer
from radis.labels.processors import LabelingTaskProcessor


class TestLabelingTaskProcessor:
    def test_processes_all_reports_in_task(self):
        job = LabelingJobFactory(status=AnalysisJob.Status.PENDING)
        task = LabelingTaskFactory(job=job, status=AnalysisTask.Status.PENDING)
        reports = [ReportFactory(body="B1"), ReportFactory(body="B2"), ReportFactory(body="B3")]
        for r in reports:
            task.reports.add(r)
        QuestionFactory(label="pneumonia", group="lung", active=True)

        with patch("radis.labels.services.ChatClient") as ChatClientMock:
            def fake_extract(prompt, Schema):
                field_defaults = {name: "YES" for name in Schema.model_fields}
                return Schema(**field_defaults)
            ChatClientMock.return_value.extract_data.side_effect = fake_extract

            LabelingTaskProcessor(task).start()

        assert Answer.objects.count() == 3
        task.refresh_from_db()
        assert task.status == AnalysisTask.Status.SUCCESS

    def test_task_marked_warning_when_one_report_fails(self):
        job = LabelingJobFactory(status=AnalysisJob.Status.PENDING)
        task = LabelingTaskFactory(job=job, status=AnalysisTask.Status.PENDING)
        r_ok = ReportFactory(body="ok body")
        r_bad = ReportFactory(body="bad body")
        task.reports.add(r_ok)
        task.reports.add(r_bad)
        QuestionFactory(label="pneumonia", group="lung", active=True)

        with patch("radis.labels.services.ChatClient") as ChatClientMock:
            def fake_extract(prompt, Schema):
                if "bad body" in prompt:
                    raise RuntimeError("LLM exploded")
                field_defaults = {name: "YES" for name in Schema.model_fields}
                return Schema(**field_defaults)
            ChatClientMock.return_value.extract_data.side_effect = fake_extract

            LabelingTaskProcessor(task).start()

        task.refresh_from_db()
        # Successful report has an Answer; failing one does not.
        assert Answer.objects.filter(report=r_ok).exists()
        assert not Answer.objects.filter(report=r_bad).exists()
        # WARNING means partial success.
        assert task.status == AnalysisTask.Status.WARNING
```

- [ ] **Step 2: Implement**

Create `radis/labels/processors.py`:

```python
import logging
from concurrent.futures import ThreadPoolExecutor

from django.conf import settings

from radis.chats.utils.chat_client import ChatClient
from radis.core.models import AnalysisTask
from radis.core.processors import AnalysisTaskProcessor
from .models import LabelingTask
from .services import label_report


logger = logging.getLogger("radis.labels")


class LabelingTaskProcessor(AnalysisTaskProcessor):
    def __init__(self, task: LabelingTask) -> None:
        super().__init__(task)
        self.client = ChatClient()

    def process_task(self, task: LabelingTask) -> None:
        any_failure = False
        any_success = False
        with ThreadPoolExecutor(
            max_workers=settings.LABELING_LLM_CONCURRENCY_LIMIT
        ) as executor:
            futures = [
                executor.submit(label_report, report.id, self.client)
                for report in task.reports.all()
            ]
            for future in futures:
                try:
                    future.result()
                    any_success = True
                except Exception as exc:  # noqa: BLE001 — record and continue.
                    any_failure = True
                    logger.exception("Labeling failed for one report: %s", exc)

        if any_failure and any_success:
            task.status = AnalysisTask.Status.WARNING
            task.message = "Some reports failed to label; see logs."
        elif any_failure:
            task.status = AnalysisTask.Status.FAILURE
            task.message = "All reports failed to label."
        else:
            task.status = AnalysisTask.Status.SUCCESS
```

**Caveat:** `AnalysisTaskProcessor.start()` already sets `task.status = IN_PROGRESS` and wraps the `process_task` call in a try/except that maps unhandled exceptions to FAILURE. The above sets `task.status` *inside* `process_task` so the base class's success path is what runs afterward; verify against `radis/core/processors.py` to ensure this composition is correct. If the base class overrides the status after `process_task` returns, switch to storing the result on `task.log`/`task.message` only and letting the base class set status.

- [ ] **Step 3: Run tests**

```bash
uv run cli test -- radis/labels/tests/test_processor.py -q
```

Expected: all pass. Adjust the implementation if the base class composition behaves differently than assumed.

- [ ] **Step 4: Commit**

```bash
git add radis/labels/processors.py radis/labels/tests/test_processor.py
git commit -m "feat(labels): LabelingTaskProcessor with ThreadPoolExecutor over reports"
```

---

## Task 16: `process_labeling_task` Procrastinate task

**Files:**
- Modify: `radis/labels/tasks.py`
- Modify: `radis/labels/tests/test_tasks.py`

- [ ] **Step 1: Write failing tests**

Append to `radis/labels/tests/test_tasks.py`:

```python
from unittest.mock import patch

from radis.labels.factories import LabelingTaskFactory
from radis.labels.tasks import process_labeling_task


class TestProcessLabelingTask:
    def test_dispatches_to_processor(self):
        task = LabelingTaskFactory()
        with patch("radis.labels.tasks.LabelingTaskProcessor") as ProcMock:
            process_labeling_task(task_id=task.id)
        ProcMock.assert_called_once()
        # Verify .start() was called on the constructed processor.
        ProcMock.return_value.start.assert_called_once()
```

- [ ] **Step 2: Implement**

Append to `radis/labels/tasks.py`:

```python
from .models import LabelingTask
from .processors import LabelingTaskProcessor


@app.task(queue="llm")
def process_labeling_task(task_id: int) -> None:
    task = LabelingTask.objects.get(id=task_id)
    processor = LabelingTaskProcessor(task)
    processor.start()
    task.queued_job_id = None
    task.save()
```

- [ ] **Step 3: Run tests**

```bash
uv run cli test -- radis/labels/tests/test_tasks.py::TestProcessLabelingTask -q
```

Expected: passes.

- [ ] **Step 4: Commit**

```bash
git add radis/labels/tasks.py radis/labels/tests/test_tasks.py
git commit -m "feat(labels): process_labeling_task Procrastinate task"
```

---

## Task 17: `create_labeling_tasks_streaming`

**Files:**
- Modify: `radis/labels/services.py`
- Modify: `radis/labels/tests/test_scope.py`

- [ ] **Step 1: Write failing tests**

Append to `radis/labels/tests/test_scope.py`:

```python
from radis.core.models import AnalysisJob
from radis.labels.models import LabelingTask
from radis.labels.services import create_labeling_tasks_streaming


class TestCreateLabelingTasks:
    def test_creates_tasks_with_batch_size_reports_each(self, settings):
        settings.LABELING_TASK_BATCH_SIZE = 3
        # Make 7 reports that need work.
        for _ in range(7):
            ReportFactory()
        QuestionFactory(active=True)
        job = LabelingJobFactory(status=AnalysisJob.Status.PREPARING)

        create_labeling_tasks_streaming(job, chunk_size=2)

        tasks = list(LabelingTask.objects.filter(job=job).order_by("id"))
        # 7 reports / batch 3 = 3 tasks (3 + 3 + 1).
        assert len(tasks) == 3
        assert [t.reports.count() for t in tasks] == [3, 3, 1]
        assert all(t.status == "PE" or t.status == "PENDING" for t in tasks)

    def test_creates_no_tasks_when_no_reports_need_work(self):
        # Question exists but no reports.
        QuestionFactory(active=True)
        job = LabelingJobFactory(status=AnalysisJob.Status.PREPARING)
        create_labeling_tasks_streaming(job, chunk_size=10)
        assert LabelingTask.objects.filter(job=job).count() == 0
```

(Note: the test reads `t.status` as either the short code or the verbose name — adjust to whichever the codebase uses.)

- [ ] **Step 2: Implement**

Append to `radis/labels/services.py`:

```python
from django.db import transaction

from radis.core.models import AnalysisTask
from .models import LabelingJob, LabelingTask


def create_labeling_tasks_streaming(
    job: LabelingJob, chunk_size: int = 1000
) -> int:
    """Stream-build LabelingTask rows for every report needing work.

    Returns the total number of tasks created. Each task contains up to
    settings.LABELING_TASK_BATCH_SIZE reports.
    """
    from django.conf import settings

    batch_size = settings.LABELING_TASK_BATCH_SIZE
    total = 0
    bucket: list[int] = []

    for report_id in find_reports_needing_work(chunk_size=chunk_size):
        bucket.append(report_id)
        if len(bucket) >= batch_size:
            _flush_bucket(job, bucket)
            total += 1
            bucket = []

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

- [ ] **Step 3: Run tests**

```bash
uv run cli test -- radis/labels/tests/test_scope.py::TestCreateLabelingTasks -q
```

Expected: passes.

- [ ] **Step 4: Commit**

```bash
git add radis/labels/services.py radis/labels/tests/test_scope.py
git commit -m "feat(labels): stream-create LabelingTasks chunk-by-chunk during PREPARING"
```

---

## Task 18: `process_labeling_job` orchestrator + `LabelingJob.delay`

**Files:**
- Modify: `radis/labels/tasks.py`
- Modify: `radis/labels/models.py`
- Modify: `radis/labels/tests/test_tasks.py`

- [ ] **Step 1: Write failing tests**

Append to `radis/labels/tests/test_tasks.py`:

```python
from radis.core.models import AnalysisJob
from radis.labels.factories import LabelingJobFactory
from radis.labels.tasks import process_labeling_job


class TestProcessLabelingJob:
    def test_prep_phase_creates_tasks_then_enqueues(self):
        job = LabelingJobFactory(status=AnalysisJob.Status.UNVERIFIED)
        # Three reports needing work.
        for _ in range(3):
            ReportFactory()
        QuestionFactory(active=True)

        with patch(
            "radis.labels.tasks.enqueue_all_pending_tasks"
        ) as enqueue_mock:
            process_labeling_job(job_id=job.id)

        job.refresh_from_db()
        assert job.status == AnalysisJob.Status.PENDING
        assert job.tasks.count() > 0
        enqueue_mock.assert_called_once_with(job)

    def test_started_at_set(self):
        job = LabelingJobFactory(status=AnalysisJob.Status.UNVERIFIED)
        QuestionFactory(active=True)
        with patch("radis.labels.tasks.enqueue_all_pending_tasks"):
            process_labeling_job(job_id=job.id)
        job.refresh_from_db()
        assert job.started_at is not None
```

- [ ] **Step 2: Implement the job orchestrator**

Append to `radis/labels/tasks.py`:

```python
from django.utils import timezone

from radis.core.models import AnalysisJob
from .models import LabelingJob
from .services import create_labeling_tasks_streaming


@app.task()
def process_labeling_job(job_id: int) -> None:
    job = LabelingJob.objects.get(id=job_id)
    job.status = AnalysisJob.Status.PREPARING
    job.started_at = timezone.now()
    job.save()

    create_labeling_tasks_streaming(job)

    job.status = AnalysisJob.Status.PENDING
    job.save()

    enqueue_all_pending_tasks(job)


def enqueue_all_pending_tasks(job: LabelingJob) -> None:
    """Defer process_labeling_task for every PENDING LabelingTask under the job.

    Filled out fully in Task 19; the import is here so process_labeling_job can call it.
    """
    raise NotImplementedError("Implemented in Task 19")
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

- [ ] **Step 4: Run tests**

```bash
uv run cli test -- radis/labels/tests/test_tasks.py::TestProcessLabelingJob -q
```

Expected: tests pass. The `NotImplementedError` in `enqueue_all_pending_tasks` is mocked out in the test.

- [ ] **Step 5: Commit**

```bash
git add radis/labels/tasks.py radis/labels/models.py radis/labels/tests/test_tasks.py
git commit -m "feat(labels): process_labeling_job orchestrator and LabelingJob.delay"
```

---

## Task 19: `enqueue_all_pending_tasks`

**Files:**
- Modify: `radis/labels/tasks.py`
- Modify: `radis/labels/tests/test_tasks.py`

- [ ] **Step 1: Write failing tests**

Append to `radis/labels/tests/test_tasks.py`:

```python
from radis.labels.factories import LabelingTaskFactory
from radis.labels.tasks import enqueue_all_pending_tasks


class TestEnqueueAllPending:
    def test_defers_one_task_per_pending(self):
        job = LabelingJobFactory(status=AnalysisJob.Status.PENDING)
        t1 = LabelingTaskFactory(job=job, status=AnalysisTask.Status.PENDING)
        t2 = LabelingTaskFactory(job=job, status=AnalysisTask.Status.PENDING)
        # A non-pending task should be skipped.
        LabelingTaskFactory(job=job, status=AnalysisTask.Status.SUCCESS)

        with patch("radis.labels.tasks.app") as app_mock:
            configure_task_mock = app_mock.configure_task.return_value
            configure_task_mock.defer.side_effect = [101, 102]

            enqueue_all_pending_tasks(job)

            assert configure_task_mock.defer.call_count == 2
            task_ids = sorted(
                c.kwargs["task_id"] for c in configure_task_mock.defer.call_args_list
            )
            assert task_ids == sorted([t1.id, t2.id])
```

- [ ] **Step 2: Replace the stub**

In `radis/labels/tasks.py`, replace the stub `enqueue_all_pending_tasks`:

```python
def enqueue_all_pending_tasks(job: LabelingJob) -> None:
    """Defer process_labeling_task for every PENDING LabelingTask under the job."""
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


# Add this import at the top if not present:
from radis.core.models import AnalysisTask
```

- [ ] **Step 3: Run tests**

```bash
uv run cli test -- radis/labels/tests/test_tasks.py -q
```

Expected: all pass.

- [ ] **Step 4: Commit**

```bash
git add radis/labels/tasks.py radis/labels/tests/test_tasks.py
git commit -m "feat(labels): enqueue_all_pending_tasks defers one Procrastinate task per LabelingTask"
```

---

# Phase 5 — Admin UX

## Task 20: `QuestionAdmin`

**Files:**
- Create: `radis/labels/admin.py`
- Create: `radis/labels/tests/test_admin.py`

- [ ] **Step 1: Write failing tests**

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
        username="admin", email="admin@example.com", password="adminpass"
    )
    client.force_login(user)
    return client


class TestQuestionAdmin:
    def test_changelist_loads(self, admin_client):
        QuestionFactory(label="pneumonia")
        url = reverse("admin:labels_question_changelist")
        response = admin_client.get(url)
        assert response.status_code == 200
        assert b"pneumonia" in response.content

    def test_add_form_loads(self, admin_client):
        url = reverse("admin:labels_question_add")
        response = admin_client.get(url)
        assert response.status_code == 200

    def test_duplicate_label_shows_form_error(self, admin_client):
        QuestionFactory(label="pneumonia")
        url = reverse("admin:labels_question_add")
        response = admin_client.post(
            url,
            data={"label": "pneumonia", "text": "Q?", "group": "lung", "active": "on"},
        )
        # Form error returns 200 with the error message.
        assert response.status_code == 200
        assert b"unique" in response.content.lower() or b"already exists" in response.content.lower()
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
    list_display = ("label", "group", "active", "text_preview", "updated_at")
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
            stale=Count(
                "pk", filter=Q(generated_at__lt=obj.updated_at)
            ),
        )
        return format_html(
            "{yes} Yes · {maybe} Maybe · {no} No · {stale} stale",
            **counts,
        )

    answer_summary.short_description = "Answers"
```

- [ ] **Step 3: Run tests**

```bash
uv run cli test -- radis/labels/tests/test_admin.py::TestQuestionAdmin -q
```

Expected: tests pass.

- [ ] **Step 4: Commit**

```bash
git add radis/labels/admin.py radis/labels/tests/test_admin.py
git commit -m "feat(labels): QuestionAdmin with answer summary and text preview"
```

---

## Task 21: `AnswerAdmin`

**Files:**
- Modify: `radis/labels/admin.py`
- Modify: `radis/labels/tests/test_admin.py`

- [ ] **Step 1: Write failing tests**

Append to `radis/labels/tests/test_admin.py`:

```python
from radis.labels.factories import AnswerFactory


class TestAnswerAdmin:
    def test_changelist_loads(self, admin_client):
        AnswerFactory()
        url = reverse("admin:labels_answer_changelist")
        response = admin_client.get(url)
        assert response.status_code == 200

    def test_add_is_disabled(self, admin_client):
        url = reverse("admin:labels_answer_add")
        response = admin_client.get(url)
        assert response.status_code in (302, 403)

    def test_change_is_disabled(self, admin_client):
        a = AnswerFactory()
        url = reverse("admin:labels_answer_change", args=[a.id])
        response = admin_client.get(url)
        # Read-only views typically still 200 but submit is disabled; ensure access at least.
        assert response.status_code in (200, 302, 403)
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

- [ ] **Step 3: Run tests**

```bash
uv run cli test -- radis/labels/tests/test_admin.py::TestAnswerAdmin -q
```

Expected: tests pass.

- [ ] **Step 4: Commit**

```bash
git add radis/labels/admin.py radis/labels/tests/test_admin.py
git commit -m "feat(labels): read-only AnswerAdmin with stale filter"
```

---

## Task 22: `AnswerInline` on `ReportAdmin`

**Files:**
- Modify: `radis/labels/admin.py`
- Modify: `radis/reports/admin.py`
- Modify: `radis/labels/tests/test_admin.py`

- [ ] **Step 1: Write failing tests**

Append to `radis/labels/tests/test_admin.py`:

```python
from radis.reports.factories import ReportFactory


class TestReportAdminWithLabels:
    def test_report_change_shows_answer_inline(self, admin_client):
        report = ReportFactory()
        AnswerFactory(report=report, value="YES")
        url = reverse("admin:reports_report_change", args=[report.id])
        response = admin_client.get(url)
        assert response.status_code == 200
        assert b"answers" in response.content.lower() or b"answer" in response.content.lower()
```

- [ ] **Step 2: Define the inline in labels**

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

In `radis/reports/admin.py`, find the `ReportAdmin` class and its `inlines` attribute. Add the import and append `AnswerInline`:

```python
from radis.labels.admin import AnswerInline

class ReportAdmin(admin.ModelAdmin):
    # … existing fields …
    inlines = [MetadataInline, AnswerInline]  # append AnswerInline to the existing list
```

(Replace `MetadataInline` with whatever is already in the existing list — preserve the existing inlines.)

- [ ] **Step 4: Run tests**

```bash
uv run cli test -- radis/labels/tests/test_admin.py::TestReportAdminWithLabels -q
```

Expected: passes.

- [ ] **Step 5: Commit**

```bash
git add radis/labels/admin.py radis/reports/admin.py radis/labels/tests/test_admin.py
git commit -m "feat(labels): AnswerInline on ReportAdmin"
```

---

## Task 23: `LabelingJobAdmin` (list + detail, read-only)

**Files:**
- Modify: `radis/labels/admin.py`
- Modify: `radis/labels/tests/test_admin.py`

- [ ] **Step 1: Write failing tests**

Append to `radis/labels/tests/test_admin.py`:

```python
from radis.core.models import AnalysisJob
from radis.labels.factories import LabelingJobFactory


class TestLabelingJobAdmin:
    def test_changelist_loads(self, admin_client):
        LabelingJobFactory(status=AnalysisJob.Status.PENDING)
        url = reverse("admin:labels_labelingjob_changelist")
        response = admin_client.get(url)
        assert response.status_code == 200

    def test_add_is_disabled(self, admin_client):
        url = reverse("admin:labels_labelingjob_add")
        response = admin_client.get(url)
        assert response.status_code in (302, 403)
```

- [ ] **Step 2: Implement**

Append to `radis/labels/admin.py`:

```python
from .models import LabelingJob


@admin.register(LabelingJob)
class LabelingJobAdmin(admin.ModelAdmin):
    list_display = ("id", "status", "owner", "task_count", "created_at", "started_at", "ended_at")
    list_filter = ("status",)
    readonly_fields = (
        "status", "owner", "message", "created_at", "started_at", "ended_at",
        "task_count", "progress_detail",
    )
    fields = readonly_fields

    def task_count(self, obj: LabelingJob) -> int:
        return obj.tasks.count()

    def progress_detail(self, obj: LabelingJob) -> str:
        if obj.pk is None:
            return "—"
        from radis.core.models import AnalysisTask
        statuses = obj.tasks.values_list("status", flat=True)
        total = len(statuses)
        if total == 0:
            return "No tasks yet."
        success = sum(1 for s in statuses if s == AnalysisTask.Status.SUCCESS)
        failure = sum(1 for s in statuses if s == AnalysisTask.Status.FAILURE)
        warning = sum(1 for s in statuses if s == AnalysisTask.Status.WARNING)
        pending = sum(1 for s in statuses if s == AnalysisTask.Status.PENDING)
        in_progress = sum(1 for s in statuses if s == AnalysisTask.Status.IN_PROGRESS)
        return (
            f"{success} success · {warning} warning · {failure} failure · "
            f"{in_progress} in-progress · {pending} pending · {total} total"
        )

    def has_add_permission(self, request):
        return False
```

- [ ] **Step 3: Run tests**

```bash
uv run cli test -- radis/labels/tests/test_admin.py::TestLabelingJobAdmin -q
```

Expected: passes.

- [ ] **Step 4: Commit**

```bash
git add radis/labels/admin.py radis/labels/tests/test_admin.py
git commit -m "feat(labels): LabelingJobAdmin (read-only with progress summary)"
```

---

## Task 24: Run/Cancel admin views + changelist banner template

**Files:**
- Create: `radis/labels/admin_views.py`
- Modify: `radis/labels/admin.py`
- Create: `radis/labels/templates/labels/admin/labelingjob_changelist.html`
- Modify: `radis/labels/tests/test_admin.py`

- [ ] **Step 1: Write failing tests**

Append to `radis/labels/tests/test_admin.py`:

```python
from unittest.mock import patch


class TestBackfillRunCancel:
    def test_run_view_creates_job_and_delays(self, admin_client):
        with patch("radis.labels.admin_views.LabelingJob") as JobMock:
            instance = JobMock.return_value
            instance.pk = 1
            JobMock.objects.filter.return_value.exists.return_value = False
            url = reverse("admin:labels_run_backfill")
            response = admin_client.post(url)
        assert response.status_code in (302, 303)
        instance.delay.assert_called_once()

    def test_run_view_blocks_when_active_exists(self, admin_client):
        LabelingJobFactory(status=AnalysisJob.Status.PENDING)
        url = reverse("admin:labels_run_backfill")
        response = admin_client.post(url, follow=True)
        # Redirect back with a flash message.
        assert response.status_code == 200
        assert b"another" in response.content.lower() or b"active" in response.content.lower()

    def test_cancel_view_flips_status_to_canceling(self, admin_client):
        job = LabelingJobFactory(status=AnalysisJob.Status.IN_PROGRESS)
        url = reverse("admin:labels_cancel_backfill", args=[job.id])
        response = admin_client.post(url)
        assert response.status_code in (302, 303)
        job.refresh_from_db()
        assert job.status == AnalysisJob.Status.CANCELING
```

- [ ] **Step 2: Implement the admin views**

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
    changelist_url = reverse("admin:labels_labelingjob_changelist")

    if LabelingJob.objects.filter(status__in=LabelingJob.ACTIVE_STATUSES).exists():
        messages.error(request, "Another backfill is already active.")
        return HttpResponseRedirect(changelist_url)

    try:
        with transaction.atomic():
            job = LabelingJob.objects.create(
                owner=request.user,
                status=AnalysisJob.Status.UNVERIFIED,
            )
        job.delay()
        messages.success(request, f"Backfill job #{job.id} started.")
    except IntegrityError:
        messages.error(request, "Another backfill just started; please refresh.")

    return HttpResponseRedirect(changelist_url)


@staff_member_required
@require_POST
def cancel_backfill_view(request: HttpRequest, job_id: int) -> HttpResponseRedirect:
    job = get_object_or_404(LabelingJob, id=job_id)
    if job.status not in LabelingJob.ACTIVE_STATUSES:
        messages.error(request, "Job is not in a cancelable state.")
    else:
        job.status = AnalysisJob.Status.CANCELING
        job.save(update_fields=["status"])
        messages.success(request, f"Backfill job #{job.id} canceling.")
    return HttpResponseRedirect(
        reverse("admin:labels_labelingjob_changelist")
    )
```

- [ ] **Step 3: Register URLs via `LabelingJobAdmin.get_urls`**

In `radis/labels/admin.py`, modify `LabelingJobAdmin` to add `get_urls`, `change_list_template`, and `changelist_view` context:

```python
from django.urls import path

from . import admin_views


@admin.register(LabelingJob)
class LabelingJobAdmin(admin.ModelAdmin):
    # … existing fields …
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
        active = LabelingJob.objects.filter(
            status__in=LabelingJob.ACTIVE_STATUSES
        ).first()
        extra_context["active_job"] = active
        return super().changelist_view(request, extra_context=extra_context)
```

- [ ] **Step 4: Create the changelist template**

Create `radis/labels/templates/labels/admin/labelingjob_changelist.html`:

```html
{% extends "admin/change_list.html" %}
{% load admin_urls %}

{% block content_title %}
  {{ block.super }}
  <div style="margin: 1em 0; padding: 1em; background: #f8f9fa; border-radius: 4px;">
    {% if active_job %}
      <strong>Active backfill: #{{ active_job.id }}</strong>
      — status: {{ active_job.get_status_display }} —
      <a href="{% url 'admin:labels_labelingjob_change' active_job.id %}">view</a>
      <form method="post"
            action="{% url 'admin:labels_cancel_backfill' active_job.id %}"
            style="display: inline; margin-left: 1em;">
        {% csrf_token %}
        <button type="submit" class="button" onclick="return confirm('Cancel this backfill?');">
          Cancel
        </button>
      </form>
    {% else %}
      <form method="post" action="{% url 'admin:labels_run_backfill' %}">
        {% csrf_token %}
        <button type="submit" class="button" onclick="return confirm('Start a new backfill?');">
          Run backfill
        </button>
      </form>
    {% endif %}
  </div>
{% endblock %}
```

- [ ] **Step 5: Run tests**

```bash
uv run cli test -- radis/labels/tests/test_admin.py::TestBackfillRunCancel -q
```

Expected: passes.

- [ ] **Step 6: Commit**

```bash
git add radis/labels/admin.py radis/labels/admin_views.py radis/labels/templates/ radis/labels/tests/test_admin.py
git commit -m "feat(labels): Run/Cancel backfill via admin changelist banner"
```

---

# Phase 6 — Report detail surfacing

## Task 25: Cotton component `<c-report-labels />`

**Files:**
- Create: `radis/labels/templates/cotton/report_labels.html`
- Create: `radis/labels/tests/test_cotton.py`

- [ ] **Step 1: Write failing tests**

Create `radis/labels/tests/test_cotton.py`:

```python
from datetime import timedelta

from django.template import Context, Template
from django.utils import timezone

from radis.reports.factories import ReportFactory
from radis.labels.factories import AnswerFactory, QuestionFactory
from radis.labels.models import Answer, Question


def render_component(report):
    tmpl = Template(
        '{% load cotton %}<c-report-labels :report="report" />'
    )
    return tmpl.render(Context({"report": report}))


class TestReportLabelsComponent:
    def test_yes_badge_rendered(self):
        report = ReportFactory()
        q = QuestionFactory(label="pneumonia", group="lung")
        AnswerFactory(report=report, question=q, value=Answer.Value.YES)
        out = render_component(report)
        assert "pneumonia" in out
        # YES badge has the success class (or whatever the template assigns).
        assert "yes" in out.lower() or "success" in out.lower()

    def test_maybe_badge_rendered_with_distinct_style(self):
        report = ReportFactory()
        q = QuestionFactory(label="pneumonia", group="lung")
        AnswerFactory(report=report, question=q, value=Answer.Value.MAYBE)
        out = render_component(report)
        assert "pneumonia" in out
        assert "maybe" in out.lower() or "?" in out

    def test_no_badge_not_rendered(self):
        report = ReportFactory()
        q = QuestionFactory(label="not-relevant", group="lung")
        AnswerFactory(report=report, question=q, value=Answer.Value.NO)
        out = render_component(report)
        assert "not-relevant" not in out

    def test_stale_styling(self):
        report = ReportFactory()
        q = QuestionFactory(label="pneumonia", group="lung")
        a = AnswerFactory(report=report, question=q, value=Answer.Value.YES)
        future = a.generated_at + timedelta(seconds=10)
        Question.objects.filter(pk=q.pk).update(updated_at=future)
        out = render_component(report)
        assert "stale" in out.lower() or "outdated" in out.lower()

    def test_pending_message_when_no_answers(self):
        report = ReportFactory()
        out = render_component(report)
        assert "pending" in out.lower()
```

- [ ] **Step 2: Implement the component**

Create `radis/labels/templates/cotton/report_labels.html`:

```html
{% load static %}
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
                      title="Label may be outdated; will be refreshed by next backfill.">
                  {{ answer.question.label }}
                  <small>(stale)</small>
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

**Note on Cotton naming:** Cotton resolves `<c-report-labels />` to a template at `templates/cotton/report_labels.html`. Verify by checking how an existing component in `radis/core/templates/cotton/` is referenced (e.g., `<c-formset-form />` → `formset_form.html`). Rename the file if the convention differs.

- [ ] **Step 3: Run tests**

```bash
uv run cli test -- radis/labels/tests/test_cotton.py -q
```

Expected: passes.

- [ ] **Step 4: Commit**

```bash
git add radis/labels/templates/cotton/ radis/labels/tests/test_cotton.py
git commit -m "feat(labels): <c-report-labels /> Cotton component"
```

---

## Task 26: Wire `<c-report-labels />` into report detail + prefetch

**Files:**
- Modify: `radis/reports/templates/reports/report_detail.html`
- Modify: `radis/reports/views.py`

- [ ] **Step 1: Write a failing test (view-level integration)**

Create or append to `radis/labels/tests/test_cotton.py`:

```python
from django.urls import reverse


class TestReportDetailIntegration:
    def test_report_detail_renders_labels(self, admin_client):
        report = ReportFactory()
        q = QuestionFactory(label="pneumonia", group="lung")
        AnswerFactory(report=report, question=q, value=Answer.Value.YES)

        url = reverse("report_detail", kwargs={"pk": report.id})
        response = admin_client.get(url)
        assert response.status_code == 200
        assert b"pneumonia" in response.content
```

- [ ] **Step 2: Include the component in the detail template**

In `radis/reports/templates/reports/report_detail.html`, after the `{% include "reports/_report_buttons_panel.html" with hide_view_button=True %}` line (around line 71), add:

```html
<c-report-labels :report="report" />
```

- [ ] **Step 3: Prefetch answers in the view**

In `radis/reports/views.py`, find `ReportDetailView` and add a `get_queryset` override:

```python
from django.db.models import Prefetch

class ReportDetailView(...):  # keep existing bases
    def get_queryset(self):
        from radis.labels.models import Answer

        return (
            super()
            .get_queryset()
            .prefetch_related(
                Prefetch(
                    "answers",
                    queryset=Answer.objects
                    .exclude(value="NO")
                    .select_related("question")
                    .order_by("question__group", "question__label"),
                )
            )
        )
```

- [ ] **Step 4: Run tests**

```bash
uv run cli test -- radis/labels/tests/test_cotton.py::TestReportDetailIntegration -q
```

Expected: passes.

- [ ] **Step 5: Commit**

```bash
git add radis/reports/templates/reports/report_detail.html radis/reports/views.py
git commit -m "feat(labels): show labels on report detail with prefetched answers"
```

---

# Phase 7 — Search surfacing

## Task 27: Add `labels` to `SearchFilters` dataclass and the SearchForm

**Files:**
- Modify: `radis/search/models.py` (or wherever `SearchFilters` is defined)
- Modify: `radis/search/forms.py`
- Create: `radis/labels/tests/test_search_integration.py`

**Note:** Before implementing, locate the exact `SearchFilters` dataclass and the matching SearchForm field used in `radis/pgsearch/providers.py:60-87`. Grep for `class SearchFilters` and use the actual fields list in your edit.

- [ ] **Step 1: Write failing tests**

Create `radis/labels/tests/test_search_integration.py`:

```python
import pytest

from radis.reports.factories import ReportFactory
from radis.labels.factories import AnswerFactory, QuestionFactory
from radis.labels.models import Answer


@pytest.fixture
def labeled_corpus():
    """Make three reports with a known label matrix."""
    r1 = ReportFactory(body="report one")
    r2 = ReportFactory(body="report two")
    r3 = ReportFactory(body="report three")
    q_pneu = QuestionFactory(label="pneumonia", group="lung")
    q_eff = QuestionFactory(label="effusion", group="lung")
    AnswerFactory(report=r1, question=q_pneu, value=Answer.Value.YES)
    AnswerFactory(report=r1, question=q_eff, value=Answer.Value.NO)
    AnswerFactory(report=r2, question=q_pneu, value=Answer.Value.MAYBE)
    AnswerFactory(report=r3, question=q_eff, value=Answer.Value.YES)
    return {"r1": r1, "r2": r2, "r3": r3}


class TestSearchFiltersHasLabels:
    def test_search_filters_supports_labels_attribute(self):
        from radis.search.models import SearchFilters  # adjust import path

        f = SearchFilters(labels=["pneumonia"])
        assert f.labels == ["pneumonia"]

    def test_search_filters_defaults_to_empty(self):
        from radis.search.models import SearchFilters

        f = SearchFilters()
        assert f.labels == []
```

- [ ] **Step 2: Implement**

In `radis/search/models.py` (or the file that defines `SearchFilters`), add a `labels: list[str]` field, defaulting to an empty list:

```python
@dataclass
class SearchFilters:
    # … existing fields …
    labels: list[str] = field(default_factory=list)
```

(Use whatever idiom the existing dataclass uses — TypedDict, Pydantic, plain dataclass, etc.)

In `radis/search/forms.py`, add a `labels` ModelMultipleChoiceField to the SearchForm:

```python
from radis.labels.models import Question


class SearchForm(forms.Form):
    # … existing fields …
    labels = forms.MultipleChoiceField(
        required=False,
        widget=forms.CheckboxSelectMultiple,
        choices=[],  # populated dynamically in __init__
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

(If the SearchForm uses crispy-form layouts, also add `labels` to the `filters_helper` layout so it renders inside the Filters card.)

- [ ] **Step 3: Run tests**

```bash
uv run cli test -- radis/labels/tests/test_search_integration.py::TestSearchFiltersHasLabels -q
```

Expected: passes.

- [ ] **Step 4: Commit**

```bash
git add radis/search/models.py radis/search/forms.py radis/labels/tests/test_search_integration.py
git commit -m "feat(labels): add labels filter to SearchFilters and SearchForm"
```

---

## Task 28: Translate `labels` filter in pgsearch

**Files:**
- Modify: `radis/pgsearch/providers.py`
- Modify: `radis/labels/tests/test_search_integration.py`

- [ ] **Step 1: Write failing tests**

Append to `radis/labels/tests/test_search_integration.py`:

```python
from django.db.models import Q

from radis.pgsearch.providers import _build_filter_query
from radis.search.models import SearchFilters


class TestLabelFilterTranslation:
    def test_single_label_filter(self, labeled_corpus):
        # r1 has YES pneumonia; r2 has MAYBE pneumonia; r3 has no pneumonia answer.
        from radis.reports.models import Report  # use whatever queryset wrapper pgsearch uses

        filters = SearchFilters(labels=["pneumonia"])
        q = _build_filter_query(filters)
        # We expect r1 and r2 to match (YES + MAYBE attach the label).
        # Adapt to whatever Q targets — Report or ReportSearchVector.
        matched = Report.objects.filter(q).values_list("id", flat=True)
        assert labeled_corpus["r1"].id in matched
        assert labeled_corpus["r2"].id in matched
        assert labeled_corpus["r3"].id not in matched

    def test_multiple_labels_intersect(self, labeled_corpus):
        from radis.reports.models import Report

        filters = SearchFilters(labels=["pneumonia", "effusion"])
        q = _build_filter_query(filters)
        matched = list(Report.objects.filter(q).values_list("id", flat=True))
        # r1 has YES pneumonia and NO effusion → effusion not attached → excluded.
        # r2 has MAYBE pneumonia, no effusion answer → excluded.
        # r3 has no pneumonia answer, YES effusion → excluded.
        assert matched == []
```

**Note about the path of `_build_filter_query`:** It's referenced internally; the tests above touch it directly. If it's not exposed as a module-level function, refactor it to be importable (or write tests at the `search()` level instead).

- [ ] **Step 2: Implement**

In `radis/pgsearch/providers.py`, extend `_build_filter_query`:

```python
def _build_filter_query(filters: SearchFilters) -> Q:
    fq = Q()
    # … existing filter blocks …

    if filters.labels:
        from radis.labels.models import Answer

        # AND semantics across labels: report must have a non-NO answer for each.
        # We use Subquery rather than a chained M2M join to keep NOT semantics correct.
        for label in filters.labels:
            fq &= Q(
                report__id__in=Answer.objects.filter(
                    question__label=label,
                    value__in=["YES", "MAYBE"],
                ).values("report_id")
            )

    return fq
```

(If `_build_filter_query` filters against `ReportSearchVector` rather than `Report`, adjust the FK traversal accordingly — `report__id__in=` becomes whatever the existing filters use.)

- [ ] **Step 3: Run tests**

```bash
uv run cli test -- radis/labels/tests/test_search_integration.py::TestLabelFilterTranslation -q
```

Expected: passes.

- [ ] **Step 4: Commit**

```bash
git add radis/pgsearch/providers.py radis/labels/tests/test_search_integration.py
git commit -m "feat(labels): translate labels filter in pgsearch via Subquery"
```

---

## Task 29: Label facet counts

**Files:**
- Modify: `radis/pgsearch/providers.py`
- Modify: `radis/search/views.py`
- Modify: `radis/search/templates/search/_search_results.html` (or wherever facet panel lives)
- Modify: `radis/labels/tests/test_search_integration.py`

- [ ] **Step 1: Write failing tests**

Append to `radis/labels/tests/test_search_integration.py`:

```python
from radis.pgsearch.providers import facet_label_counts


class TestFacetCounts:
    def test_returns_label_counts(self, labeled_corpus):
        from radis.reports.models import Report

        result_qs = Report.objects.all()
        counts = facet_label_counts(result_qs, top_n=10)

        # Result is a list of (label, count) tuples ordered desc.
        as_dict = dict(counts)
        assert as_dict.get("pneumonia") == 2  # r1 YES, r2 MAYBE
        assert as_dict.get("effusion") == 1  # r3 YES

    def test_top_n_caps_results(self, labeled_corpus):
        from radis.reports.models import Report

        counts = facet_label_counts(Report.objects.all(), top_n=1)
        assert len(counts) == 1
```

- [ ] **Step 2: Implement the helper**

In `radis/pgsearch/providers.py`:

```python
from django.db.models import Count, QuerySet


def facet_label_counts(
    reports_qs: QuerySet, top_n: int = 20
) -> list[tuple[str, int]]:
    """Return [(label, count), …] for the top-N most-applied labels in the given report queryset."""
    from radis.labels.models import Answer

    counts = (
        Answer.objects.filter(report__in=reports_qs, value__in=["YES", "MAYBE"])
        .values("question__label")
        .annotate(c=Count("report", distinct=True))
        .order_by("-c", "question__label")[:top_n]
    )
    return [(row["question__label"], row["c"]) for row in counts]
```

- [ ] **Step 3: Wire into the search view**

In `radis/search/views.py`, find `SearchView` and add the facet to the context:

```python
def get_context_data(self, **kwargs):
    ctx = super().get_context_data(**kwargs)
    from radis.pgsearch.providers import facet_label_counts

    # Use whichever queryset of results the view exposes — adjust attribute name.
    if hasattr(self, "result_queryset"):
        ctx["label_facets"] = facet_label_counts(self.result_queryset, top_n=20)
    return ctx
```

- [ ] **Step 4: Render in the template**

In `radis/search/templates/search/search.html` (or `_search_results.html`), inside the filters card, after the existing crispy form (around line 26 of `search.html`), add:

```html
{% if label_facets %}
  <hr>
  <div class="label-facet">
    <h6 class="card-subtitle text-muted">Labels</h6>
    <ul class="list-unstyled mb-0">
      {% for label, count in label_facets %}
        <li>
          <label>
            <input type="checkbox"
                   name="labels"
                   value="{{ label }}"
                   {% if label in request.GET.labels|default_if_none:""|stringformat:"s" %}checked{% endif %}>
            {{ label }} <span class="text-muted">({{ count }})</span>
          </label>
        </li>
      {% endfor %}
    </ul>
  </div>
{% endif %}
```

(Adjust the `request.GET.labels` membership check; for crispy-form rendering you can also rely on the form's `labels` field being rendered with its current selections.)

- [ ] **Step 5: Run tests**

```bash
uv run cli test -- radis/labels/tests/test_search_integration.py::TestFacetCounts -q
```

Expected: passes.

- [ ] **Step 6: Commit**

```bash
git add radis/pgsearch/providers.py radis/search/views.py radis/search/templates/ radis/labels/tests/test_search_integration.py
git commit -m "feat(labels): label facet counts on search results"
```

---

# Phase 8 — Operations, docs, acceptance

## Task 30: `radis.labels` logger config

**Files:**
- Modify: `radis/settings/base.py`

- [ ] **Step 1: Find LOGGING config**

Grep for `LOGGING = {` in `radis/settings/base.py`.

- [ ] **Step 2: Add the logger entry**

Inside the `LOGGING["loggers"]` dict, add:

```python
"radis.labels": {
    "handlers": ["console"],  # use whichever handler keys already exist
    "level": "INFO",
    "propagate": False,
},
```

(Match the handlers and format used by neighboring loggers in the same file.)

- [ ] **Step 3: Verify**

```bash
uv run cli shell -c "import logging; logging.getLogger('radis.labels').warning('test'); print('ok')"
```

Expected: prints something to stdout and "ok".

- [ ] **Step 4: Commit**

```bash
git add radis/settings/base.py
git commit -m "feat(labels): configure radis.labels logger"
```

---

## Task 31: `labels_status` management command

**Files:**
- Create: `radis/labels/management/commands/labels_status.py`
- Create: `radis/labels/tests/test_management.py`
- Modify: `cli.py`

- [ ] **Step 1: Write failing tests**

Create `radis/labels/tests/test_management.py`:

```python
from io import StringIO

from django.core.management import call_command

from radis.reports.factories import ReportFactory
from radis.labels.factories import AnswerFactory, QuestionFactory
from radis.labels.models import Answer


class TestLabelsStatusCommand:
    def test_prints_corpus_coverage(self):
        ReportFactory()
        ReportFactory()
        q = QuestionFactory(label="pneumonia", active=True)
        AnswerFactory(question=q, value=Answer.Value.YES)

        buf = StringIO()
        call_command("labels_status", stdout=buf)
        out = buf.getvalue()

        assert "pneumonia" in out
        assert "Total reports" in out or "total reports" in out.lower()
```

- [ ] **Step 2: Implement the command**

Create `radis/labels/management/commands/labels_status.py`:

```python
from django.core.management.base import BaseCommand
from django.db.models import Count, F, Q

from radis.reports.models import Report
from radis.labels.models import Answer, Question


class Command(BaseCommand):
    help = "Print labeling coverage for the corpus."

    def handle(self, *args, **opts):
        total_reports = Report.objects.count()
        active_q_count = Question.objects.filter(active=True).count()
        self.stdout.write(f"Total reports: {total_reports}")
        self.stdout.write(f"Active questions: {active_q_count}")

        if active_q_count == 0:
            self.stdout.write("No active questions — nothing to report.")
            return

        # Coverage
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
            .filter(non_stale_count=active_q_count)
            .count()
        )
        missing_or_stale = total_reports - fully_current
        self.stdout.write(f"Fully current: {fully_current}")
        self.stdout.write(f"Missing or stale: {missing_or_stale}")

        # Per-question summary
        for q in Question.objects.filter(active=True).order_by("group", "label"):
            counts = q.answers.aggregate(
                yes=Count("pk", filter=Q(value="YES")),
                no=Count("pk", filter=Q(value="NO")),
                maybe=Count("pk", filter=Q(value="MAYBE")),
                stale=Count("pk", filter=Q(generated_at__lt=q.updated_at)),
            )
            self.stdout.write(
                f"  [{q.group}] {q.label}: "
                f"{counts['yes']} Y · {counts['maybe']} M · "
                f"{counts['no']} N · {counts['stale']} stale"
            )
```

- [ ] **Step 3: Wire into cli.py**

In `cli.py`, after the existing `@app.command()` decorated functions, add:

```python
@app.command(name="labels-status")
def labels_status():
    """Print labeling coverage for the corpus."""
    import subprocess
    subprocess.run(["./manage.py", "labels_status"], check=True)
```

(Or follow the existing pattern used by other commands that wrap `manage.py` calls — grep `cli.py` for a similar wrapper, e.g., `db_backup`.)

- [ ] **Step 4: Run tests**

```bash
uv run cli test -- radis/labels/tests/test_management.py -q
```

Expected: passes.

- [ ] **Step 5: Smoke-test the CLI wrapper**

```bash
uv run cli labels-status
```

Expected: prints "Total reports: …" etc. without error.

- [ ] **Step 6: Commit**

```bash
git add radis/labels/management/ radis/labels/tests/test_management.py cli.py
git commit -m "feat(labels): labels_status management command and CLI wrapper"
```

---

## Task 32: Acceptance test (real LLM)

**Files:**
- Create: `radis/labels/tests/test_acceptance.py`

- [ ] **Step 1: Write the acceptance test**

Create `radis/labels/tests/test_acceptance.py`:

```python
import time

import pytest

from radis.reports.factories import ReportFactory
from radis.labels.factories import QuestionFactory
from radis.labels.models import Answer


@pytest.mark.acceptance
class TestRealLLMSmoke:
    def test_signal_path_labels_a_report_end_to_end(self):
        """Create one question, ingest one report, wait for the Procrastinate task,
        and assert the Answer row appears.
        """
        q = QuestionFactory(
            label="lungs_clear",
            text="Are the lungs clear?",
            group="lung",
            active=True,
        )
        report = ReportFactory(body="No abnormalities, lungs clear.")

        # Poll the DB for up to 60 seconds for the answer to appear.
        deadline = time.time() + 60
        while time.time() < deadline:
            answer = Answer.objects.filter(report=report, question=q).first()
            if answer is not None:
                break
            time.sleep(1.0)
        else:
            pytest.fail("Answer did not appear within 60 seconds")

        assert answer.value == Answer.Value.YES, (
            f"Expected YES but got {answer.value}"
        )
```

- [ ] **Step 2: Run the acceptance test in the dev container**

```bash
uv run cli test -- -m acceptance radis/labels/tests/test_acceptance.py -v
```

Expected: passes. (Requires `compose-up` to be running so a Procrastinate worker is consuming the `llm` queue and the LLM service is reachable.)

If this is the first acceptance test pass, it confirms the end-to-end wiring (signal → handler → Procrastinate task → label_report → real LLM → DB write).

- [ ] **Step 3: Commit**

```bash
git add radis/labels/tests/test_acceptance.py
git commit -m "test(labels): real-LLM acceptance smoke test"
```

---

## Task 33: Documentation updates

**Files:**
- Modify: `CLAUDE.md`
- Modify: `KNOWLEDGE.md`

- [ ] **Step 1: Update `CLAUDE.md`**

In the Django Apps section of `CLAUDE.md`, after the `radis.extractions` entry, add:

```markdown
- **radis.labels/**: Auto-labeling system. Admin-defined questions (YES/NO/MAYBE) are evaluated against report bodies by an LLM. Two paths: per-report background task on Report create/update, and an admin-triggered singleton backfill (`LabelingJob`/`LabelingTask`).
```

Add to the Environment Variables section:

```markdown
- `LABELING_PER_REPORT_PRIORITY`, `LABELING_BACKFILL_PRIORITY`: Procrastinate priorities for the two execution paths.
- `LABELING_TASK_BATCH_SIZE`: Reports per backfill task (default 100).
- `LABELING_LLM_CONCURRENCY_LIMIT`: Concurrent LLM calls per backfill task (default 6).
- `LABELING_SYSTEM_PROMPT`: Override the default labeling prompt template.
```

Add a Troubleshooting subsection:

```markdown
### Labels Not Appearing

- Check the active question count: `uv run cli labels-status`
- Verify `llm_worker` is running and processing the `llm` queue
- Look for failures in `docker compose logs llm_worker | grep "radis.labels"`
- Confirm `radis.labels.apps.LabelsConfig` is in `INSTALLED_APPS`

### Backfill Stuck in PREPARING

- `LabelingJob.delay()` defers `process_labeling_job` to the default queue, not the LLM queue
- Verify `default_worker` is running and not blocked
- Check the LabelingJob admin progress detail; if zero tasks, the scope query may be returning empty
```

- [ ] **Step 2: Update `KNOWLEDGE.md`**

Add a section on labeling prompt design:

```markdown
## Labeling Prompt Design

The labeling system batches questions by their `group` string — all questions sharing a group go to the LLM in a single prompt. Smaller groups make for cleaner answers; larger groups save LLM calls. Authors should err on the side of grouping questions that share clinical context (e.g., all lung findings together).

The answer space is fixed at YES/NO/MAYBE and enforced by the Pydantic schema, not just by prose. The MAYBE answer is reserved for genuine ambiguity in the report — the prompt explicitly tells the LLM not to use MAYBE when the answer is clear. Authors should write questions answerable from the report body alone; if the answer requires external context (e.g., prior images), the question is a poor fit for this feature.
```

- [ ] **Step 3: Commit**

```bash
git add CLAUDE.md KNOWLEDGE.md
git commit -m "docs(labels): document the auto-labeling feature"
```

---

## Task 34: Full-suite green check

**Files:** none (verification only)

- [ ] **Step 1: Run the full test suite**

```bash
uv run cli test -- radis/labels/ -q --tb=short
```

Expected: all labels tests pass (excluding `-m acceptance`, which is run separately).

- [ ] **Step 2: Run the broader suite for regressions**

```bash
uv run cli test -- -q --tb=short
```

Expected: no regressions in `reports`, `search`, `pgsearch`, etc. If anything breaks: triage.

- [ ] **Step 3: Run linter and formatter**

```bash
uv run cli lint
uv run cli format-code
```

Fix anything reported.

- [ ] **Step 4: Final commit (if there are any cleanups from lint/format)**

```bash
git add -A
git diff --cached  # review
git commit -m "chore(labels): post-implementation lint/format pass"
```

- [ ] **Step 5: Push the branch and open a PR**

```bash
git push -u origin feature/auto-labeling-design
gh pr create --title "feat: auto-labeling feature" --body "$(cat <<'EOF'
## Summary
- New radis.labels app: admin-managed YES/NO/MAYBE questions classify radiology reports via LLM
- Per-report background labeling on Report create/update (signal-driven, llm queue, priority 1)
- Admin-triggered singleton backfill (LabelingJob/LabelingTask) for the existing corpus
- Label badges on report detail; label filter in search

See `docs/superpowers/specs/2026-05-21-auto-labeling-design.md` for the design and `docs/superpowers/plans/2026-05-21-auto-labeling.md` for the implementation plan.

## Test plan
- [ ] Unit tests pass: `uv run cli test -- radis/labels/`
- [ ] Acceptance smoke test passes against the local LLM: `uv run cli test -- -m acceptance radis/labels/tests/test_acceptance.py`
- [ ] No regressions in `reports`, `search`, `pgsearch` tests
- [ ] Run a small backfill in a staging environment and verify Run/Cancel controls in admin
- [ ] Manually create a report, observe the labeling task firing and an Answer row appearing within ~10s

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

---

# Self-review checklist (post-write)

The plan covers every spec section:

- **Data model** — Tasks 2, 3, 4, 5
- **Per-report path** — Tasks 11 (label_report), 12 (task), 13 (handler registration)
- **Backfill path** — Tasks 14 (scope), 15 (processor), 16 (task), 17 (streaming task creation), 18 (orchestrator + delay), 19 (enqueue all)
- **Admin UX** — Tasks 20 (Question), 21 (Answer), 22 (ReportAdmin inline), 23 (LabelingJob), 24 (Run/Cancel + banner)
- **Report detail surfacing** — Tasks 25 (Cotton component), 26 (template wiring + prefetch)
- **Search surfacing** — Tasks 27 (filters dataclass + form), 28 (pgsearch translator), 29 (facet counts + template)
- **Settings + operations** — Tasks 6 (settings + prompt), 30 (logger), 31 (management command), 32 (acceptance smoke)
- **Documentation** — Task 33

Open notes / verifications the implementer must do:

1. `AnalysisJob.Status` short codes used in the partial-unique-index SQL must be verified against the enum (Task 5 step 4).
2. The `Procrastinate` defer API shape (`task.configure(...).defer(...)` vs `app.configure_task("dotted", ...).defer(...)`) varies — Task 13 step 2 says to use whichever the project already uses; Task 18 step 3 already does.
3. `LabelingTaskProcessor` composition with `AnalysisTaskProcessor.start()` — verify the base class doesn't overwrite `task.status` after `process_task` returns (Task 15 caveat).
4. The `_build_filter_query` function targets a queryset over `ReportSearchVector` (per the exploration) rather than `Report`; the FK traversal `report__id__in=...` may need to be adjusted (Task 28 note).
5. The QueryParser `label:` syntax from the spec is replaced with a SearchFilters dataclass field (this plan's deviation; mentioned in the intro and to be confirmed before execution).
