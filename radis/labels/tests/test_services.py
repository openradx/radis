import time

from radis.labels.factories import QuestionFactory
from radis.labels.models import Answer
from radis.labels.prompts import sanitize_label
from radis.labels.services import group_active_questions_by_group, upsert_answers
from radis.reports.factories import ReportFactory


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
