from __future__ import annotations

import logging
import math
import time
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from string import Template

from django import db
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from radis.chats.utils.chat_client import ChatClient
from radis.reports.models import Report

from .models import Answer, AnswerOption, LabelingRun, Question, QuestionSet
from .schemas import QuestionSetSchema
from .utils.processor_utils import (
    build_labeling_response_schema,
    question_set_schema_for_run,
    render_questions_block,
)

logger = logging.getLogger(__name__)


class LabelingProcessor:
    """Run labelling for one ``QuestionSet`` over a batch of reports in one mode.

    Currently implements the DIRECT mode (single structured-output call per
    report). The REASONED mode (two-call) is added in the dual-mode commit;
    the processor's per-mode shape is designed so adding it doesn't require
    re-plumbing the surrounding fanout/exception/persistence logic.
    """

    def __init__(self, question_set: QuestionSet, mode: str = LabelingRun.Mode.DIRECT) -> None:
        self.question_set = question_set
        self.mode = mode
        self.client = ChatClient()
        self._model_name = settings.LLM_MODEL_NAME

    def process_reports(self, report_ids: list[int]) -> None:
        if not report_ids:
            return

        active_questions = list(
            self.question_set.questions.filter(is_active=True).prefetch_related("options")
        )
        if not active_questions:
            logger.info("No active questions for set %s", self.question_set)
            return

        # MEDIUM #1 fix. Drop active questions whose Pydantic schema cannot
        # be built before any LLM work is scheduled. The commonest cause is
        # an active question with zero AnswerOptions — its strict
        # ``Literal`` enum would be ``Literal[()]`` which Pydantic refuses
        # to construct. Pre-fix the schema build raised at the top of
        # ``_run_llm_and_persist``, taking the entire report's
        # ``transaction.atomic`` block down with it; one misconfigured
        # question broke labelling for every other question in the batch.
        #
        # The principle is the user's: if a Pydantic schema can't be built
        # for a question, the question is invalid and excluded from this
        # run. The other questions in the set still get answered. The
        # invalid question stays unanswered (and stays in ``missing_reports()``
        # until its configuration is fixed) — visible to ops via the WARN
        # log per drop. We DO NOT mark the question inactive on disk: the
        # invariant we enforce is "don't schedule" not "punish data".
        questions: list[Question] = []
        for question in active_questions:
            if not list(question.options.all()):
                logger.warning(
                    "Skipping active question %s in set %s for this run — "
                    "no AnswerOptions configured. The question will stay "
                    "unanswered (and the report unfinished) until options "
                    "are added.",
                    question,
                    self.question_set,
                )
                continue
            questions.append(question)

        if not questions:
            logger.warning(
                "All active questions in set %s are invalid (no options). "
                "Skipping labelling for reports %s.",
                self.question_set,
                report_ids,
            )
            return

        # Build the canonical schema mirror, then prune to the valid
        # questions so ``build_answer_schema`` / ``render_questions_for_prompt``
        # use the same enumeration order the processor will read responses
        # from. The schema and the processor iterate by index; keeping
        # both filtered to the same set is what makes ``question_{index}``
        # lookups in the response object resolve to the right Question.
        schema_mirror = question_set_schema_for_run(self.question_set)
        valid_labels = {q.label for q in questions}
        schema_mirror.questions = [
            q for q in schema_mirror.questions if q.label in valid_labels
        ]

        option_maps, unknown_options = _build_option_maps(questions)

        reports = Report.objects.filter(id__in=report_ids).only("id", "body")

        with ThreadPoolExecutor(max_workers=settings.LABELING_LLM_CONCURRENCY_LIMIT) as executor:
            futures: list[Future] = []
            future_report_ids: dict[Future, int] = {}
            try:
                for report in reports:
                    future = executor.submit(
                        self._process_report,
                        report,
                        questions,
                        option_maps,
                        unknown_options,
                        schema_mirror,
                    )
                    futures.append(future)
                    future_report_ids[future] = report.id

                for future in as_completed(futures):
                    try:
                        future.result()
                    except Exception:
                        report_id = future_report_ids.get(future)
                        logger.exception(
                            "Labeling failed for report %s in set %s (mode=%s)",
                            report_id,
                            self.question_set,
                            self.mode,
                        )
            finally:
                db.close_old_connections()

    def _process_report(
        self,
        report: Report,
        questions: list[Question],
        option_maps: dict[int, dict[str, AnswerOption]],
        unknown_options: dict[int, AnswerOption | None],
        schema_mirror: QuestionSetSchema,
    ) -> None:
        # A run row represents the LLM exchange. Create it up-front in PENDING
        # so a crash mid-call still leaves an attributable record; subsequent
        # state transitions land via an explicit UPDATE.
        run = LabelingRun.objects.create(
            report=report,
            question_set=self.question_set,
            mode=self.mode,
            status=LabelingRun.Status.IN_PROGRESS,
            model_name=self._model_name,
        )

        try:
            self._run_llm_and_persist(
                run, report, questions, option_maps, unknown_options, schema_mirror
            )
        except Exception as exc:
            LabelingRun.objects.filter(pk=run.pk).update(
                status=LabelingRun.Status.FAILURE,
                error_message=str(exc)[:4000],
                completed_at=timezone.now(),
            )
            raise
        finally:
            db.close_old_connections()

    def _run_llm_and_persist(
        self,
        run: LabelingRun,
        report: Report,
        questions: list[Question],
        option_maps: dict[int, dict[str, AnswerOption]],
        unknown_options: dict[int, AnswerOption | None],
        schema_mirror: QuestionSetSchema,
    ) -> None:
        response_schema = build_labeling_response_schema(schema_mirror)
        questions_block = render_questions_block(schema_mirror)

        started = time.monotonic()

        # REASONED mode is a two-call sequence: free-form reasoning first
        # (so chain-of-thought is not constrained by the structured-output
        # schema), then a structured call that lands the actual choices
        # given the report + reasoning as context.
        reasoning_text = ""
        if self.mode == LabelingRun.Mode.REASONED:
            reasoning_prompt = Template(settings.LABELS_REASONING_PROMPT).substitute(
                {"report": report.body, "questions": questions_block}
            )
            reasoning_text = self.client.complete_text(reasoning_prompt.strip())

            structured_prompt = Template(
                settings.LABELS_REASONED_STRUCTURED_PROMPT
            ).substitute(
                {
                    "report": report.body,
                    "questions": questions_block,
                    "reasoning": reasoning_text,
                }
            )
        else:
            structured_prompt = Template(settings.LABELS_SYSTEM_PROMPT).substitute(
                {"report": report.body, "questions": questions_block}
            )

        result = self.client.extract_data(structured_prompt.strip(), response_schema)
        latency_ms = int((time.monotonic() - started) * 1000)

        # Persist run output and answers atomically so we never end up with
        # a SUCCESS run whose answers failed to write.
        with transaction.atomic():
            for index, question in enumerate(questions):
                field_name = f"question_{index}"
                answer = getattr(result, field_name)
                option = _resolve_option(
                    answer.choice,
                    option_maps[question.id],
                    unknown_options.get(question.id),
                )
                Answer.objects.create(
                    run=run,
                    report=report,
                    question=question,
                    question_version=question.version,
                    option=option,
                    confidence=_normalize_confidence(answer.confidence),
                    rationale=(answer.rationale or "").strip(),
                    verified=False,
                )

            LabelingRun.objects.filter(pk=run.pk).update(
                status=LabelingRun.Status.SUCCESS,
                raw_response=result.model_dump(),
                reasoning_text=reasoning_text,
                latency_ms=latency_ms,
                completed_at=timezone.now(),
            )


def _build_option_maps(
    questions: list[Question],
) -> tuple[dict[int, dict[str, AnswerOption]], dict[int, AnswerOption | None]]:
    option_maps: dict[int, dict[str, AnswerOption]] = {}
    unknown_options: dict[int, AnswerOption | None] = {}

    for question in questions:
        options = list(question.options.all())
        if not options:
            logger.warning("Question %s has no answer options, skipping.", question)
            option_maps[question.id] = {}
            unknown_options[question.id] = None
            continue
        option_maps[question.id] = {option.value: option for option in options}
        unknown_options[question.id] = next((o for o in options if o.is_unknown), None)

    return option_maps, unknown_options


def _resolve_option(
    value: str,
    options: dict[str, AnswerOption],
    unknown_option: AnswerOption | None,
) -> AnswerOption:
    """Map the LLM's returned ``value`` to a persisted ``AnswerOption``.

    Resolution order:
      1. Exact match on ``value`` (the common path; strict ``Literal``
         in the response schema guarantees this branch in normal flow).
      2. Fall back to the ``is_unknown`` option if the question has one.
      3. Raise ``ValueError`` — see below.

    Pre-MEDIUM-#1 the fallback was ``next(iter(options.values()))``, which
    silently assigned an arbitrary answer when neither the exact value
    nor an ``is_unknown`` option was available. With empty-option questions
    now filtered out at ``process_reports`` entry, this fallback is
    unreachable in normal flow; we raise instead so the LabelingRun goes
    to FAILURE with a useful error message rather than silently writing
    a wrong answer to a question whose schema we couldn't honor.
    """
    option = options.get(value)
    if option is not None:
        return option
    if unknown_option is not None:
        return unknown_option
    raise ValueError(
        f"Cannot resolve LLM-returned value {value!r} for question — "
        f"no exact match and no is_unknown option configured. "
        f"This indicates a schema-options mismatch that should have been "
        f"caught at process_reports entry; treating as run failure."
    )


def _normalize_confidence(confidence: float | None) -> float | None:
    """Clamp the LLM's reported confidence to ``[0.0, 1.0]`` or ``None``.

    MEDIUM #2 fix: ``NaN`` previously passed straight through. The bounds
    checks ``confidence < 0`` and ``confidence > 1`` both return ``False``
    for NaN — IEEE 754 says NaN compares as not-less-than and not-greater-than
    everything — so the function returned NaN unchanged. Downstream the NaN
    was written to ``Answer.confidence`` and corrupted ``eval_metrics``
    mean-confidence math: ``(0.9 + 0.85 + NaN) / 3 = NaN``, and the
    rendered Markdown report had "nan" cells.

    We treat NaN the same as ``None`` — "the model didn't give us a usable
    confidence." The Answer row stores ``None`` and downstream code (which
    already handles the None branch for models that decline to report
    confidence) does the right thing.
    """
    if confidence is None or math.isnan(confidence):
        return None
    if confidence < 0:
        return 0.0
    if confidence > 1:
        return 1.0
    return confidence
