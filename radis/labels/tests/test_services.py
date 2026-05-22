import time
from datetime import timedelta
from unittest.mock import patch

from radis.labels.factories import AnswerFactory, QuestionFactory
from radis.labels.models import Answer
from radis.labels.prompts import sanitize_label
from radis.labels.services import (
    group_active_questions_by_group,
    label_report,
    upsert_answers,
)
from radis.reports.factories import ReportFactory
from radis.reports.models import Report


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
            ChatClientMock.return_value.extract_data.side_effect = lambda prompt, Schema: Schema(
                **{f: "YES" for f in Schema.model_fields}
            )
            label_report(r.id)
            assert ChatClientMock.return_value.extract_data.call_count == 2

    def test_persists_answers(self):
        r = ReportFactory(body="b")
        q = QuestionFactory(label="x", group="g", active=True)
        with patch("radis.labels.services.ChatClient") as ChatClientMock:
            ChatClientMock.return_value.extract_data.side_effect = lambda prompt, Schema: Schema(
                x="MAYBE"
            )
            label_report(r.id)
        assert Answer.objects.get(report=r, question=q).value == "MAYBE"

    def test_skips_currently_labeled_group(self):
        """Per-group idempotency: when all answers in a group are current, no LLM call fires."""
        r = ReportFactory(body="b")
        q = QuestionFactory(label="x", group="g", active=True)
        # Pre-populate a current answer.
        AnswerFactory(report=r, question=q, value="YES")
        # Make report.updated_at slightly older than answer.generated_at.
        Report.objects.filter(pk=r.pk).update(
            updated_at=Answer.objects.get(report=r).generated_at - timedelta(seconds=1)
        )
        with patch("radis.labels.services.ChatClient") as ChatClientMock:
            label_report(r.id)
        ChatClientMock.assert_not_called()
