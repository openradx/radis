import logging
from collections import defaultdict
from datetime import datetime
from typing import Mapping

from radis.chats.utils.chat_client import ChatClient
from radis.reports.models import Report

from .models import Answer, Question
from .prompts import build_yes_no_maybe_schema, render_questions_prompt, sanitize_label

logger = logging.getLogger("radis.labels")


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


def _group_answers_are_current(
    questions: list[Question],
    existing: dict[int, Answer],
    report_updated_at: datetime,
) -> bool:
    for q in questions:
        a = existing.get(q.id)
        if a is None:
            return False
        if a.generated_at < q.updated_at:
            return False
        if a.generated_at < report_updated_at:
            return False
    return True


def label_report(report_id: int, client: ChatClient | None = None) -> None:
    report = Report.objects.get(id=report_id)
    if not report.body or not report.body.strip():
        logger.info("labels.skip empty body: report %s", report_id)
        return

    questions_by_group = group_active_questions_by_group()
    if not questions_by_group:
        logger.info("labels.skip no active questions: report %s", report_id)
        return

    existing = {
        a.question_id: a
        for a in Answer.objects.filter(report=report).select_related("question")
    }
    chat = client
    for group_str, questions in questions_by_group.items():
        if _group_answers_are_current(questions, existing, report.updated_at):
            continue
        if chat is None:
            chat = ChatClient()
        Schema = build_yes_no_maybe_schema(questions)
        prompt = render_questions_prompt(report.body, questions)
        parsed = chat.extract_data(prompt, Schema)
        upsert_answers(report, questions, parsed.model_dump())
