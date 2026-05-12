from __future__ import annotations

from itertools import batched

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from radis.reports.models import Report

from ...models import BackfillJob, LabelingRun, QuestionSet
from ...tasks import process_question_set_batch


class Command(BaseCommand):
    help = "Enqueue labeling tasks for existing reports."

    def add_arguments(self, parser):
        parser.add_argument(
            "--set",
            dest="question_set",
            help="QuestionSet name or ID. If omitted, all active sets are used.",
        )
        parser.add_argument(
            "--batch-size",
            dest="batch_size",
            type=int,
            default=None,
            help="Override the task batch size.",
        )
        parser.add_argument(
            "--limit",
            dest="limit",
            type=int,
            default=None,
            help="Limit the number of reports to enqueue.",
        )

    def handle(self, *args, **options):
        value = options.get("question_set")
        batch_size = options.get("batch_size")
        limit = options.get("limit")

        if value:
            question_sets = [self._get_set(value)]
        else:
            question_sets = list(QuestionSet.objects.filter(is_active=True))

        if not question_sets:
            self.stdout.write(self.style.WARNING("No active question sets found."))
            return

        report_ids_qs = Report.objects.order_by("id").values_list("id", flat=True)
        if limit:
            report_ids_qs = report_ids_qs[:limit]
        report_ids = list(report_ids_qs)

        if not report_ids:
            self.stdout.write(self.style.WARNING("No reports found."))
            return

        if batch_size is None:
            batch_size = settings.LABELING_TASK_BATCH_SIZE

        modes = getattr(settings, "LABELS_RUN_MODES", [LabelingRun.Mode.DIRECT])

        for question_set in question_sets:
            backfill_job = BackfillJob.objects.create(
                question_set=question_set,
                status=BackfillJob.Status.IN_PROGRESS,
                started_at=timezone.now(),
                total_reports=len(report_ids),
            )

            for report_batch in batched(report_ids, batch_size):
                batch_list = list(report_batch)
                for mode in modes:
                    process_question_set_batch.defer(
                        question_set_id=question_set.id,
                        report_ids=batch_list,
                        mode=mode,
                        backfill_job_id=backfill_job.id,
                    )

            self.stdout.write(
                self.style.SUCCESS(
                    f"Enqueued labeling for {len(report_ids)} reports "
                    f"in set '{question_set.name}' (backfill job #{backfill_job.id})."
                )
            )

    def _get_set(self, value: str) -> QuestionSet:
        if value.isdigit():
            question_set = QuestionSet.objects.filter(id=int(value)).first()
        else:
            matches = QuestionSet.objects.filter(name=value)
            if matches.count() > 1:
                raise CommandError(
                    f"Multiple question sets named '{value}' exist. Use the numeric ID."
                )
            question_set = matches.first()

        if not question_set:
            raise CommandError(f"Question set '{value}' not found.")

        return question_set
