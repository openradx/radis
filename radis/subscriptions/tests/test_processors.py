"""Tests for the subscriptions LLM accept/reject gate (processors.py).

The gate (``SubscriptionTaskProcessor.process_report``) sends the report body
plus the subscription filter questions to the LLM, receives a per-question
boolean result, and creates a ``SubscribedItem`` only if EVERY question is
answered as expected.

The LLM is mocked at the ``openai.OpenAI`` boundary with a fake that CAPTURES
the prompt and the requested schema so we can assert the report text + questions
reach the model.
"""

from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from adit_radis_shared.accounts.factories import GroupFactory, UserFactory
from adit_radis_shared.common.utils.testing_helpers import add_user_to_group
from pydantic import BaseModel, create_model

from radis.core.models import AnalysisJob, AnalysisTask
from radis.reports.factories import ReportFactory
from radis.subscriptions.factories import (
    FilterQuestionFactory,
    SubscriptionFactory,
    SubscriptionJobFactory,
    SubscriptionTaskFactory,
)
from radis.subscriptions.models import SubscribedItem, SubscriptionJob, SubscriptionTask
from radis.subscriptions.processors import SubscriptionTaskProcessor
from radis.subscriptions.utils.processor_utils import (
    generate_filter_questions_prompt,
    generate_filter_questions_schema,
    get_filter_question_field_name,
)


class _Capture:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []


def make_capturing_openai_mock(answers: dict[str, bool]) -> tuple[MagicMock, _Capture]:
    """Fake ``openai.OpenAI`` returning a parsed model built from ``answers``.

    ``answers`` maps filter question field names (``question_<pk>``) -> bool.
    The fake records every ``parse`` call (model, messages, response_format).
    """
    capture = _Capture()
    field_definitions: dict[str, Any] = {name: (bool, ...) for name in answers}
    Result = create_model("Result", **field_definitions)
    parsed = Result(**answers)

    def fake_parse(
        *, model: str, messages: Any, response_format: Any, extra_body: Any = None
    ) -> MagicMock:
        capture.calls.append(
            {"model": model, "messages": list(messages), "response_format": response_format}
        )
        return MagicMock(choices=[MagicMock(message=MagicMock(parsed=parsed))])

    openai_mock = MagicMock()
    openai_mock.beta.chat.completions.parse.side_effect = fake_parse
    return openai_mock, capture


def _make_task_with_reports(question_texts: list[str], num_reports: int = 1) -> SubscriptionTask:
    """Build a SubscriptionTask whose owner has an active group and whose
    reports are visible to that group."""
    user = UserFactory.create(is_active=True)
    group = GroupFactory.create()
    add_user_to_group(user, group, force_activate_group=True)

    subscription = SubscriptionFactory.create(owner=user, group=group)
    for text in question_texts:
        FilterQuestionFactory.create(subscription=subscription, question=text)

    # Tasks are only processed once the job is PENDING (set by
    # process_subscription_job before enqueueing). AnalysisTaskProcessor.start()
    # asserts this, so reflect the real runtime state here.
    job = SubscriptionJobFactory.create(
        subscription=subscription, owner=user, status=SubscriptionJob.Status.PENDING
    )
    task = SubscriptionTaskFactory.create(job=job)

    for _ in range(num_reports):
        report = ReportFactory.create()
        report.groups.add(group)
        task.reports.add(report)

    return task


def _field_names(task: SubscriptionTask) -> list[str]:
    return [
        get_filter_question_field_name(question)
        for question in task.job.subscription.filter_questions.order_by("pk")
    ]


# --------------------------------------------------------------------------- #
# processor_utils: schema + prompt generation
# --------------------------------------------------------------------------- #


@pytest.mark.django_db
def test_generate_filter_questions_schema_creates_boolean_fields_per_question():
    subscription = SubscriptionFactory.create()
    q1 = FilterQuestionFactory.create(subscription=subscription, question="Is there a fracture?")
    q2 = FilterQuestionFactory.create(subscription=subscription, question="Is it acute?")

    Schema = generate_filter_questions_schema(subscription.filter_questions.order_by("pk"))

    assert set(Schema.model_fields) == {
        get_filter_question_field_name(q1),
        get_filter_question_field_name(q2),
    }
    for field in Schema.model_fields.values():
        assert field.annotation is bool


@pytest.mark.django_db
def test_generate_filter_questions_prompt_enumerates_questions():
    subscription = SubscriptionFactory.create()
    q1 = FilterQuestionFactory.create(subscription=subscription, question="Is there a fracture?")
    q2 = FilterQuestionFactory.create(subscription=subscription, question="Is it acute?")

    prompt = generate_filter_questions_prompt(subscription.filter_questions.order_by("pk"))

    assert f"{get_filter_question_field_name(q1)}: Is there a fracture?" in prompt
    assert f"{get_filter_question_field_name(q2)}: Is it acute?" in prompt


# --------------------------------------------------------------------------- #
# Gate: report text + questions reach the model
# --------------------------------------------------------------------------- #


@pytest.mark.django_db(transaction=True)
def test_report_body_and_questions_reach_the_model():
    task = _make_task_with_reports(
        ["Is there a pulmonary nodule?", "Is it larger than 5mm?"], num_reports=1
    )
    report = task.reports.get()
    names = _field_names(task)

    # Reject (all False) so this test isolates the prompt/schema plumbing from
    # the accept path (covered by test_subscribed_item_created_when_all_answers_true).
    openai_mock, capture = make_capturing_openai_mock({name: False for name in names})
    with patch("openai.OpenAI", return_value=openai_mock):
        SubscriptionTaskProcessor(task).start()

    assert len(capture.calls) == 1
    call = capture.calls[0]
    assert call["messages"][0]["role"] == "user"
    sent = call["messages"][0]["content"]

    assert report.body in sent
    assert "Is there a pulmonary nodule?" in sent
    assert "Is it larger than 5mm?" in sent

    # Requested schema carries one boolean field per question.
    schema = call["response_format"]
    assert issubclass(schema, BaseModel)
    assert set(schema.model_fields) == set(names)


# --------------------------------------------------------------------------- #
# Gate: reject decision
# --------------------------------------------------------------------------- #


@pytest.mark.django_db(transaction=True)
def test_no_subscribed_item_when_any_answer_is_false():
    task = _make_task_with_reports(["q one", "q two"], num_reports=1)
    names = _field_names(task)

    # One True, one False -> rejected (expected answer defaults to YES).
    openai_mock, _ = make_capturing_openai_mock({names[0]: True, names[1]: False})
    with patch("openai.OpenAI", return_value=openai_mock):
        SubscriptionTaskProcessor(task).start()

    assert SubscribedItem.objects.count() == 0
    task.refresh_from_db()
    assert task.status == SubscriptionTask.Status.SUCCESS


@pytest.mark.django_db(transaction=True)
def test_reports_not_in_active_group_are_skipped():
    task = _make_task_with_reports(["q one"], num_reports=0)
    # Report that is NOT in the owner's active group must be filtered out.
    orphan = ReportFactory.create()
    task.reports.add(orphan)
    names = _field_names(task)

    openai_mock, capture = make_capturing_openai_mock({names[0]: False})
    with patch("openai.OpenAI", return_value=openai_mock):
        SubscriptionTaskProcessor(task).start()

    # The LLM was never consulted because no report matched the active group.
    assert len(capture.calls) == 0
    assert SubscribedItem.objects.count() == 0


# --------------------------------------------------------------------------- #
# Gate: accept decision
# --------------------------------------------------------------------------- #


@pytest.mark.django_db(transaction=True)
def test_subscribed_item_created_when_all_answers_true():
    task = _make_task_with_reports(["q one", "q two"], num_reports=1)
    report = task.reports.get()
    names = _field_names(task)

    openai_mock, _ = make_capturing_openai_mock({name: True for name in names})
    with patch("openai.OpenAI", return_value=openai_mock):
        SubscriptionTaskProcessor(task).start()

    item = SubscribedItem.objects.get()
    assert item.report == report
    assert item.subscription == task.job.subscription
    assert item.filter_results == {
        str(question.pk): True for question in task.job.subscription.filter_questions.all()
    }


@pytest.mark.django_db(transaction=True)
def test_accept_path_succeeds_and_creates_item():
    """The accept path completes cleanly: an accepted report creates exactly one
    SubscribedItem and the task finishes SUCCESS (previously the wrong field name
    raised inside the worker thread and marked the task FAILURE)."""
    task = _make_task_with_reports(["q one"], num_reports=1)
    names = _field_names(task)

    openai_mock, _ = make_capturing_openai_mock({names[0]: True})
    with patch("openai.OpenAI", return_value=openai_mock):
        SubscriptionTaskProcessor(task).start()

    task.refresh_from_db()
    assert task.status == SubscriptionTask.Status.SUCCESS
    assert SubscribedItem.objects.count() == 1


@pytest.mark.django_db(transaction=True)
def test_running_same_report_twice_creates_one_subscribed_item():
    # No filter questions and no output fields: the report is accepted without any LLM call.
    subscription = SubscriptionFactory.create()
    job = SubscriptionJobFactory.create(
        subscription=subscription, status=AnalysisJob.Status.IN_PROGRESS
    )
    task = SubscriptionTaskFactory.create(job=job, status=AnalysisTask.Status.IN_PROGRESS)
    report = ReportFactory.create()

    with patch("radis.subscriptions.processors.LLMClient"):
        processor = SubscriptionTaskProcessor(task)

    processor.process_report(report, task)
    processor.process_report(report, task)

    assert SubscribedItem.objects.filter(subscription=subscription, report=report).count() == 1
