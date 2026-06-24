"""Tests for the subscriptions LLM accept/reject gate (processors.py).

The gate (``SubscriptionTaskProcessor.process_report``) sends the report body
plus the subscription questions to the LLM, receives a per-question boolean
result, and creates a ``SubscribedItem`` only if EVERY question answered truthy.

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

from radis.reports.factories import ReportFactory
from radis.subscriptions.factories import (
    QuestionFactory,
    SubscriptionFactory,
    SubscriptionJobFactory,
    SubscriptionTaskFactory,
)
from radis.subscriptions.models import SubscribedItem, SubscriptionJob, SubscriptionTask
from radis.subscriptions.processors import SubscriptionTaskProcessor
from radis.subscriptions.utils.processor_utils import (
    generate_questions_for_prompt,
    generate_questions_schema,
)


class _Capture:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []


def make_capturing_openai_mock(answers: dict[str, bool]) -> tuple[MagicMock, _Capture]:
    """Fake ``openai.OpenAI`` returning a parsed model built from ``answers``.

    ``answers`` maps ``question_0``/``question_1``/... -> bool. The fake records
    every ``parse`` call (model, messages, response_format).
    """
    capture = _Capture()
    Result = create_model(  # type: ignore[call-overload]
        "Result", **{name: (bool, ...) for name in answers}
    )
    parsed = Result(**answers)

    def fake_parse(*, model: str, messages: Any, response_format: Any) -> MagicMock:
        capture.calls.append(
            {"model": model, "messages": list(messages), "response_format": response_format}
        )
        return MagicMock(choices=[MagicMock(message=MagicMock(parsed=parsed))])

    openai_mock = MagicMock()
    openai_mock.beta.chat.completions.parse.side_effect = fake_parse
    return openai_mock, capture


def _make_task_with_reports(
    question_texts: list[str], num_reports: int = 1
) -> SubscriptionTask:
    """Build a SubscriptionTask whose owner has an active group and whose
    reports are visible to that group."""
    user = UserFactory.create(is_active=True)
    group = GroupFactory.create()
    add_user_to_group(user, group, force_activate_group=True)

    subscription = SubscriptionFactory.create(owner=user, group=group)
    for text in question_texts:
        QuestionFactory.create(subscription=subscription, question=text)

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


# --------------------------------------------------------------------------- #
# processor_utils: schema + prompt generation
# --------------------------------------------------------------------------- #


@pytest.mark.django_db
def test_generate_questions_schema_creates_boolean_fields_per_question():
    subscription = SubscriptionFactory.create()
    QuestionFactory.create(subscription=subscription, question="Is there a fracture?")
    QuestionFactory.create(subscription=subscription, question="Is it acute?")

    Schema = generate_questions_schema(subscription.questions)

    assert set(Schema.model_fields) == {"question_0", "question_1"}
    for field in Schema.model_fields.values():
        assert field.annotation is bool


@pytest.mark.django_db
def test_generate_questions_for_prompt_enumerates_questions():
    subscription = SubscriptionFactory.create()
    QuestionFactory.create(subscription=subscription, question="Is there a fracture?")
    QuestionFactory.create(subscription=subscription, question="Is it acute?")

    prompt = generate_questions_for_prompt(subscription.questions)

    assert "question_0: Is there a fracture?" in prompt
    assert "question_1: Is it acute?" in prompt


# --------------------------------------------------------------------------- #
# Gate: report text + questions reach the model
# --------------------------------------------------------------------------- #


@pytest.mark.django_db(transaction=True)
def test_report_body_and_questions_reach_the_model():
    task = _make_task_with_reports(
        ["Is there a pulmonary nodule?", "Is it larger than 5mm?"], num_reports=1
    )
    report = task.reports.get()

    # Reject (all False) so this test isolates the prompt/schema plumbing from
    # the accept path (covered by test_subscribed_item_created_when_all_answers_true).
    openai_mock, capture = make_capturing_openai_mock(
        {"question_0": False, "question_1": False}
    )
    with patch("openai.OpenAI", return_value=openai_mock):
        SubscriptionTaskProcessor(task).start()

    assert len(capture.calls) == 1
    call = capture.calls[0]
    assert call["messages"][0]["role"] == "system"
    sent = call["messages"][0]["content"]

    assert report.body in sent
    assert "Is there a pulmonary nodule?" in sent
    assert "Is it larger than 5mm?" in sent

    # Requested schema carries one boolean field per question.
    schema = call["response_format"]
    assert issubclass(schema, BaseModel)
    assert set(schema.model_fields) == {"question_0", "question_1"}


# --------------------------------------------------------------------------- #
# Gate: reject decision
# --------------------------------------------------------------------------- #


@pytest.mark.django_db(transaction=True)
def test_no_subscribed_item_when_any_answer_is_false():
    task = _make_task_with_reports(["q one", "q two"], num_reports=1)

    # One True, one False -> rejected (gate requires ALL truthy).
    openai_mock, _ = make_capturing_openai_mock({"question_0": True, "question_1": False})
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

    openai_mock, capture = make_capturing_openai_mock({"question_0": False})
    with patch("openai.OpenAI", return_value=openai_mock):
        SubscriptionTaskProcessor(task).start()

    # The LLM was never consulted because no report matched the active group.
    assert len(capture.calls) == 0
    assert SubscribedItem.objects.count() == 0


# --------------------------------------------------------------------------- #
# Gate: accept decision -- exposes a real bug.
# --------------------------------------------------------------------------- #


@pytest.mark.django_db(transaction=True)
def test_subscribed_item_created_when_all_answers_true():
    task = _make_task_with_reports(["q one", "q two"], num_reports=1)
    report = task.reports.get()

    openai_mock, _ = make_capturing_openai_mock({"question_0": True, "question_1": True})
    with patch("openai.OpenAI", return_value=openai_mock):
        SubscriptionTaskProcessor(task).start()

    item = SubscribedItem.objects.get()
    assert item.report_id == report.pk
    assert item.subscription_id == task.job.subscription_id
    assert item.answers == {"question_0": True, "question_1": True}


@pytest.mark.django_db(transaction=True)
def test_accept_path_succeeds_and_creates_item():
    """The accept path completes cleanly: an accepted report creates exactly one
    SubscribedItem and the task finishes SUCCESS (previously the wrong field name
    raised inside the worker thread and marked the task FAILURE)."""
    task = _make_task_with_reports(["q one"], num_reports=1)

    openai_mock, _ = make_capturing_openai_mock({"question_0": True})
    with patch("openai.OpenAI", return_value=openai_mock):
        SubscriptionTaskProcessor(task).start()

    task.refresh_from_db()
    assert task.status == SubscriptionTask.Status.SUCCESS
    assert SubscribedItem.objects.count() == 1
