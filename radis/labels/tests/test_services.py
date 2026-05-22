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
