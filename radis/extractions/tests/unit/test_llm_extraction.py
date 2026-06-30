"""Tests for the extractions LLM data-extraction plumbing.

These tests mock the OpenAI client with a fake that CAPTURES the prompt and the
requested response schema so we can assert that the report text and the
configured output fields actually reach the model. They also cover output-field
schema generation (incl. the unknown-type error branch), output
parsing/persistence and job orchestration (task/instance creation).

The LLM is never called for real -- ``openai.OpenAI`` is patched at the SDK
boundary used by ``radis.chats.utils.chat_client.ChatClient``.
"""

from typing import Any, cast
from unittest.mock import MagicMock, patch

import pytest
from django.db.models import QuerySet
from pydantic import BaseModel
from pytest_mock import MockerFixture

from radis.extractions.models import (
    ExtractionInstance,
    ExtractionJob,
    ExtractionTask,
    OutputField,
    OutputType,
)
from radis.extractions.processors import ExtractionTaskProcessor
from radis.extractions.utils.processor_utils import (
    generate_output_fields_prompt,
    generate_output_fields_schema,
)
from radis.extractions.utils.testing_helpers import create_extraction_task


class _Capture:
    """Records every call made to ``beta.chat.completions.parse``."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []


def make_capturing_openai_mock(output: BaseModel) -> tuple[MagicMock, _Capture]:
    """Return a fake ``openai.OpenAI`` instance + a capture object.

    The fake's ``beta.chat.completions.parse`` records the ``model``,
    ``messages`` and ``response_format`` it was called with and returns a
    completion whose ``choices[0].message.parsed`` is ``output``.
    """
    capture = _Capture()

    def fake_parse(*, model: str, messages: Any, response_format: Any) -> MagicMock:
        capture.calls.append(
            {
                "model": model,
                "messages": list(messages),
                "response_format": response_format,
            }
        )
        return MagicMock(choices=[MagicMock(message=MagicMock(parsed=output))])

    openai_mock = MagicMock()
    openai_mock.beta.chat.completions.parse.side_effect = fake_parse
    return openai_mock, capture


# --------------------------------------------------------------------------- #
# Schema generation
# --------------------------------------------------------------------------- #


@pytest.mark.django_db
def test_generate_output_fields_schema_maps_each_output_type():
    job = create_extraction_task(num_output_fields=0, num_extraction_instances=0).job
    OutputField.objects.create(
        job=job, name="finding", description="d", output_type=OutputType.TEXT
    )
    OutputField.objects.create(
        job=job, name="size", description="d", output_type=OutputType.NUMERIC
    )
    OutputField.objects.create(
        job=job, name="malignant", description="d", output_type=OutputType.BOOLEAN
    )

    Schema = generate_output_fields_schema(job.output_fields)
    fields = Schema.model_fields

    assert set(fields) == {"finding", "size", "malignant"}
    assert fields["finding"].annotation is str
    assert fields["malignant"].annotation is bool
    # NUMERIC maps to ``float | int`` (a Union), so a float and an int validate.
    # Schema is built dynamically, so its fields aren't statically known to the type
    # checker; access them through Any-typed instances.
    instance = cast(Any, Schema(finding="x", size=3.5, malignant=True))
    assert instance.size == 3.5
    assert cast(Any, Schema(finding="x", size=7, malignant=False)).size == 7
    # All fields are required (``...``); omitting one is a validation error.
    with pytest.raises(Exception):
        Schema(finding="x")


def test_generate_output_fields_schema_raises_on_unknown_type():
    """The else-branch in generate_output_fields_schema must raise ValueError.

    No DB needed: we feed a plain object with an output_type that is not one of
    the known ``OutputType`` values through a tiny queryset-like shim.
    """

    class _Field:
        name = "weird"
        output_type = "Z"  # not TEXT/NUMERIC/BOOLEAN

    class _QS:
        def all(self):
            return [_Field()]

    with pytest.raises(ValueError, match="Unknown data type: Z"):
        # _QS duck-types the QuerySet.all() the function calls; cast for the checker.
        generate_output_fields_schema(cast(QuerySet[OutputField], _QS()))


@pytest.mark.django_db
def test_generate_output_fields_prompt_lists_name_and_description():
    job = create_extraction_task(num_output_fields=0, num_extraction_instances=0).job
    OutputField.objects.create(
        job=job, name="laterality", description="left or right", output_type=OutputType.TEXT
    )
    OutputField.objects.create(
        job=job,
        name="effusion",
        description="pleural effusion present",
        output_type=OutputType.BOOLEAN,
    )

    prompt = generate_output_fields_prompt(job.output_fields)

    assert "laterality: left or right" in prompt
    assert "effusion: pleural effusion present" in prompt


# --------------------------------------------------------------------------- #
# Prompt + schema actually reach the model (capturing mock)
# --------------------------------------------------------------------------- #


@pytest.mark.django_db(transaction=True)
def test_report_text_and_fields_reach_the_model(mocker: MockerFixture):
    task = create_extraction_task(num_output_fields=3, num_extraction_instances=1)
    instance = task.instances.get()
    report_body = instance.report.body
    field_names = list(task.job.output_fields.values_list("name", flat=True))
    field_descriptions = list(task.job.output_fields.values_list("description", flat=True))

    class Output(BaseModel):
        # The shape does not matter for capture; persistence is asserted elsewhere.
        dummy: str = "x"

    openai_mock, capture = make_capturing_openai_mock(Output())
    with patch("openai.OpenAI", return_value=openai_mock):
        ExtractionTaskProcessor(task).start()

    assert len(capture.calls) == 1
    call = capture.calls[0]

    # System prompt is the single message sent (role=system in ChatClient).
    assert len(call["messages"]) == 1
    assert call["messages"][0]["role"] == "system"
    sent_prompt = call["messages"][0]["content"]

    # The full report body must be present in the prompt.
    assert report_body in sent_prompt
    # Every configured output field (name + description) must be present.
    for name in field_names:
        assert name in sent_prompt
    for description in field_descriptions:
        assert description in sent_prompt

    # The requested schema is the dynamically generated OutputFieldsModel and it
    # must carry exactly the configured field names.
    schema = call["response_format"]
    assert issubclass(schema, BaseModel)
    assert set(schema.model_fields) == set(field_names)


# --------------------------------------------------------------------------- #
# Output parsing + persistence
# --------------------------------------------------------------------------- #


@pytest.mark.django_db(transaction=True)
def test_output_is_parsed_and_persisted_per_instance(mocker: MockerFixture):
    num_instances = 4
    task = create_extraction_task(num_output_fields=2, num_extraction_instances=num_instances)

    class Output(BaseModel):
        answer: str
        score: int

    output = Output(answer="positive", score=42)
    openai_mock, _ = make_capturing_openai_mock(output)
    with patch("openai.OpenAI", return_value=openai_mock):
        ExtractionTaskProcessor(task).start()

    instances = list(task.instances.all())
    assert len(instances) == num_instances
    for instance in instances:
        instance.refresh_from_db()
        assert instance.is_processed is True
        # ``output`` column holds the model_dump of the parsed result.
        assert instance.output == {"answer": "positive", "score": 42}
        # ``text`` was populated from the report body before processing.
        assert instance.text == instance.report.body

    task.refresh_from_db()
    assert task.status == ExtractionTask.Status.SUCCESS


@pytest.mark.django_db(transaction=True)
def test_processor_marks_task_failure_when_model_call_raises():
    task = create_extraction_task(num_output_fields=2, num_extraction_instances=2)

    openai_mock = MagicMock()
    openai_mock.beta.chat.completions.parse.side_effect = RuntimeError("llm boom")
    with patch("openai.OpenAI", return_value=openai_mock):
        ExtractionTaskProcessor(task).start()

    task.refresh_from_db()
    assert task.status == ExtractionTask.Status.FAILURE
    assert "llm boom" in (task.message or "")
    # Instances were never marked processed.
    assert not task.instances.filter(is_processed=True).exists()


# --------------------------------------------------------------------------- #
# Job / task orchestration (preparation -> task & instance creation)
# --------------------------------------------------------------------------- #


@pytest.mark.django_db
def test_process_extraction_job_creates_tasks_and_instances_in_batches(monkeypatch, settings):
    from adit_radis_shared.accounts.factories import GroupFactory, UserFactory

    from radis.extractions import site as extraction_site
    from radis.extractions.site import ExtractionRetrievalProvider
    from radis.extractions.tasks import process_extraction_job
    from radis.reports.factories import LanguageFactory, ReportFactory

    # Small batch size so 5 docs -> 3 tasks (2 + 2 + 1).
    settings.EXTRACTION_TASK_BATCH_SIZE = 2

    user = UserFactory.create(is_active=True)
    group = GroupFactory.create()
    language = LanguageFactory.create(code="en")

    job = ExtractionJob.objects.create(
        owner=user,
        group=group,
        title="Batch job",
        query="test",
        language=language,
        status=ExtractionJob.Status.PENDING,
    )

    doc_ids = [f"DOC-{i}" for i in range(5)]
    for doc_id in doc_ids:
        ReportFactory.create(document_id=doc_id)

    provider = ExtractionRetrievalProvider(
        name="dummy",
        count=lambda _s: len(doc_ids),
        retrieve=lambda _s: doc_ids,
        max_results=100,
    )
    monkeypatch.setattr(extraction_site, "extraction_retrieval_provider", provider)
    # Don't actually enqueue (no procrastinate).
    monkeypatch.setattr(ExtractionTask, "delay", lambda self: None, raising=True)

    process_extraction_job(int(job.pk))

    job.refresh_from_db()
    assert job.status == ExtractionJob.Status.PENDING

    tasks = list(job.tasks.all())
    assert len(tasks) == 3  # ceil(5 / 2)
    total_instances = ExtractionInstance.objects.filter(task__job=job).count()
    assert total_instances == 5
    # Every created instance references one of the retrieved reports.
    persisted_doc_ids = set(
        ExtractionInstance.objects.filter(task__job=job).values_list(
            "report__document_id", flat=True
        )
    )
    assert persisted_doc_ids == set(doc_ids)
