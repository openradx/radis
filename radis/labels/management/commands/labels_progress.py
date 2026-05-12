"""Compact progress dashboard for an in-flight labelling pass.

Walks over LabelingRun, Answer, and BackfillJob to show "how far along
are we" without having to query each table by hand. Designed for
``watch -n 5 'docker exec ... python manage.py labels_progress'``.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand
from django.db.models import Count

from ...models import Answer, BackfillJob, LabelingRun, QuestionSet


class Command(BaseCommand):
    help = "Show labelling progress: runs by (mode, status), answers, backfill jobs."

    def add_arguments(self, parser):
        parser.add_argument(
            "--question-set-id",
            dest="question_set_id",
            type=int,
            default=None,
            help="Restrict to a single QuestionSet. Otherwise shows all.",
        )

    def handle(self, *args, **options):
        qs_filter = {}
        if options["question_set_id"]:
            qs_filter["question_set_id"] = options["question_set_id"]
            question_set = QuestionSet.objects.get(pk=options["question_set_id"])
            self.stdout.write(self.style.SUCCESS(f"QuestionSet: {question_set.name} (#{question_set.pk})"))
            n_active = question_set.questions.filter(is_active=True).count()
            missing = question_set.missing_reports().count()
            self.stdout.write(f"  Active questions: {n_active}")
            self.stdout.write(f"  Reports still missing complete coverage: {missing}")
            self.stdout.write("")

        runs_qs = LabelingRun.objects.filter(**qs_filter)
        runs = list(
            runs_qs.values("mode", "status")
            .annotate(n=Count("id"))
            .order_by("mode", "status")
        )
        self.stdout.write("LabelingRun counts (mode / status):")
        if not runs:
            self.stdout.write("  none")
        else:
            for row in runs:
                self.stdout.write(
                    f"  {row['mode']} / {row['status']}: {row['n']}"
                )

        answer_filter = {}
        if options["question_set_id"]:
            answer_filter["run__question_set_id"] = options["question_set_id"]
        n_answers = Answer.objects.filter(**answer_filter).count()
        self.stdout.write(f"Answers persisted: {n_answers}")

        backfills = BackfillJob.objects.filter(**qs_filter).order_by("-created_at")[:5]
        if backfills:
            self.stdout.write("")
            self.stdout.write("Recent BackfillJobs:")
            for job in backfills:
                pct = job.progress_percent if job.total_reports else 0
                self.stdout.write(
                    f"  #{job.id} status={job.status} "
                    f"{job.processed_count}/{job.total_reports} ({pct}%) "
                    f"created={job.created_at:%H:%M:%S}"
                )

        fa = runs_qs.filter(status=LabelingRun.Status.FAILURE).order_by("-id").first()
        if fa is not None:
            self.stdout.write("")
            self.stdout.write(self.style.WARNING("Last failure:"))
            self.stdout.write(f"  {fa.error_message[:300]}")
