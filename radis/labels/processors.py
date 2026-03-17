from __future__ import annotations

import logging
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from string import Template

from django import db
from django.conf import settings
from django.db.models import Prefetch

from radis.chats.utils.chat_client import ChatClient
from radis.reports.models import Report

from .models import LabelChoice, LabelGroup, LabelQuestion, ReportLabel
from .utils.processor_utils import generate_labeling_schema, generate_questions_for_prompt

logger = logging.getLogger(__name__)


class LabelGroupProcessor:
    def __init__(self, group: LabelGroup) -> None:
        self.group = group
        self.client = ChatClient()

    def process_reports(self, report_ids: list[int], overwrite_existing: bool = False) -> None:
        if not report_ids:
            return

        questions = list(self.group.questions.filter(is_active=True).prefetch_related("choices"))
        if not questions:
            logger.info("No active label questions for group %s", self.group)
            return

        choice_maps, unknown_choices = _build_choice_maps(questions)

        labels_qs = ReportLabel.objects.filter(question__group=self.group)
        reports = (
            Report.objects.filter(id__in=report_ids)
            .prefetch_related(Prefetch("labels", queryset=labels_qs, to_attr="labels_for_group"))
            .only("id", "body")
        )

        with ThreadPoolExecutor(max_workers=settings.LABELING_LLM_CONCURRENCY_LIMIT) as executor:
            futures: list[Future] = []
            future_report_ids: dict[Future, int] = {}
            try:
                for report in reports:
                    future = executor.submit(
                        self._process_report,
                        report,
                        questions,
                        choice_maps,
                        unknown_choices,
                        overwrite_existing,
                    )
                    futures.append(future)
                    future_report_ids[future] = report.id

                for future in as_completed(futures):
                    try:
                        future.result()
                    except Exception:
                        report_id = future_report_ids.get(future)
                        logger.exception(
                            "Labeling failed for report %s in group %s", report_id, self.group
                        )
            finally:
                db.close_old_connections()

    def _process_report(
        self,
        report: Report,
        questions: list[LabelQuestion],
        choice_maps: dict[int, dict[str, LabelChoice]],
        unknown_choices: dict[int, LabelChoice | None],
        overwrite_existing: bool,
    ) -> None:
        if overwrite_existing:
            missing_questions = questions
        else:
            labels_for_group = getattr(report, "labels_for_group", [])
            existing_question_ids = {label.question_id for label in labels_for_group}
            missing_questions = [
                question for question in questions if question.id not in existing_question_ids
            ]
        missing_questions = [
            question for question in missing_questions if choice_maps.get(question.id)
        ]
        if not missing_questions:
            return

        schema = generate_labeling_schema(missing_questions)
        prompt = Template(settings.LABELS_SYSTEM_PROMPT).substitute(
            {
                "report": report.body,
                "questions": generate_questions_for_prompt(missing_questions),
            }
        )

        result = self.client.extract_data(prompt.strip(), schema)

        for index, question in enumerate(missing_questions):
            field_name = f"question_{index}"
            answer = getattr(result, field_name)
            choice = _resolve_choice(
                answer.choice,
                choice_maps[question.id],
                unknown_choices.get(question.id),
            )
            confidence = _normalize_confidence(answer.confidence)
            rationale = (answer.rationale or "").strip()

            ReportLabel.objects.update_or_create(
                report=report,
                question=question,
                defaults={
                    "choice": choice,
                    "confidence": confidence,
                    "rationale": rationale,
                    "verified": False,
                },
            )

        db.close_old_connections()


def _build_choice_maps(
    questions: list[LabelQuestion],
) -> tuple[dict[int, dict[str, LabelChoice]], dict[int, LabelChoice | None]]:
    choice_maps: dict[int, dict[str, LabelChoice]] = {}
    unknown_choices: dict[int, LabelChoice | None] = {}

    for question in questions:
        choices = list(question.choices.all())
        if not choices:
            logger.warning("LabelQuestion %s has no choices, skipping.", question)
            choice_maps[question.id] = {}
            unknown_choices[question.id] = None
            continue
        choice_maps[question.id] = {choice.value: choice for choice in choices}
        unknown_choice = next((choice for choice in choices if choice.is_unknown), None)
        unknown_choices[question.id] = unknown_choice

    return choice_maps, unknown_choices


def _resolve_choice(
    value: str,
    choices: dict[str, LabelChoice],
    unknown_choice: LabelChoice | None,
) -> LabelChoice:
    choice = choices.get(value)
    if choice is not None:
        return choice
    if unknown_choice is not None:
        return unknown_choice
    return next(iter(choices.values()))


def _normalize_confidence(confidence: float | None) -> float | None:
    if confidence is None:
        return None
    if confidence < 0:
        return 0.0
    if confidence > 1:
        return 1.0
    return confidence
