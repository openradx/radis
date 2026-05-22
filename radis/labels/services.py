from collections import defaultdict
from typing import Mapping

from radis.reports.models import Report

from .models import Answer, Question
from .prompts import sanitize_label


def group_active_questions_by_group() -> dict[str, list[Question]]:
    grouped: dict[str, list[Question]] = defaultdict(list)
    for q in Question.objects.filter(active=True).order_by("group", "label"):
        grouped[q.group].append(q)
    return dict(grouped)


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
