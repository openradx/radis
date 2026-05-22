from collections import defaultdict

from .models import Question


def group_active_questions_by_group() -> dict[str, list[Question]]:
    grouped: dict[str, list[Question]] = defaultdict(list)
    for q in Question.objects.filter(active=True).order_by("group", "label"):
        grouped[q.group].append(q)
    return dict(grouped)
