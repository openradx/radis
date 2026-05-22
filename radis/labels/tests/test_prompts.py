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
