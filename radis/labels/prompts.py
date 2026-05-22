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
