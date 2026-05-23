import logging
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Iterable, Iterator, Mapping

from django import db
from django.conf import settings
from django.db import transaction
from django.db.models import Count, F, Q

from radis.chats.utils.chat_client import ChatClient
from radis.core.models import AnalysisJob, AnalysisTask
from radis.reports.models import Report

from .models import Answer, LabelingJob, LabelingTask, Question
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
        a = existing.get(q.pk)
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
        a.question_id: a for a in Answer.objects.filter(report=report).select_related("question")
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


def label_reports_in_parallel(
    report_ids: list[int], client: ChatClient | None = None
) -> tuple[int, int]:
    chat = client or ChatClient()
    success = failure = 0

    def _worker(rid: int) -> None:
        # Each worker runs in its own thread with its own DB connection.
        # Close it on the way out so the connection doesn't linger and
        # block test-DB teardown.
        try:
            label_report(rid, chat)
        finally:
            db.close_old_connections()

    try:
        with ThreadPoolExecutor(
            max_workers=settings.LABELING_LLM_CONCURRENCY_LIMIT
        ) as executor:
            futures = [executor.submit(_worker, rid) for rid in report_ids]
            for f in futures:
                try:
                    f.result()
                    success += 1
                except Exception as exc:  # noqa: BLE001 — log and continue.
                    logger.exception("labels.report.failed: %s", exc)
                    failure += 1
    finally:
        db.close_old_connections()
    return success, failure


def find_reports_needing_work(scope_ids: Iterable[int]) -> Iterator[int]:
    active_question_count = Question.objects.filter(active=True).count()
    if active_question_count == 0:
        return iter(())
    qs = (
        Report.objects.filter(id__in=scope_ids)
        .annotate(
            non_stale_count=Count(
                "answers",
                filter=Q(
                    answers__question__active=True,
                    answers__generated_at__gte=F("answers__question__updated_at"),
                ),
            )
        )
        .filter(non_stale_count__lt=active_question_count)
        .values_list("id", flat=True)
    )
    return qs.iterator()


def create_labeling_tasks_streaming(job: LabelingJob) -> int:
    batch_size = settings.LABELING_TASK_BATCH_SIZE
    total = 0
    bucket: list[int] = []

    scope_iter = find_reports_needing_work(
        Report.objects.order_by("pk").values_list("id", flat=True)
    )

    for report_id in scope_iter:
        bucket.append(report_id)
        if len(bucket) >= batch_size:
            _flush_bucket(job, bucket)
            total += 1
            bucket = []
            # Cancellation check between buckets.
            current_status = (
                LabelingJob.objects.filter(pk=job.pk).values_list("status", flat=True).first()
            )
            if current_status == AnalysisJob.Status.CANCELING:
                return total

    if bucket:
        _flush_bucket(job, bucket)
        total += 1

    return total


def _flush_bucket(job: LabelingJob, report_ids: list[int]) -> None:
    with transaction.atomic():
        task = LabelingTask.objects.create(job=job, status=AnalysisTask.Status.PENDING)
        task.reports.set(report_ids)
