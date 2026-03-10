from __future__ import annotations

from itertools import batched

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from radis.reports.models import Report

from ...models import LabelBackfillJob, LabelGroup
from ...tasks import process_label_group


class Command(BaseCommand):
    help = "Enqueue labeling tasks for existing reports."

    def add_arguments(self, parser):
        parser.add_argument(
            "--group",
            dest="group",
            help="Label group name or ID. If omitted, all active groups are used.",
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
        group_value = options.get("group")
        batch_size = options.get("batch_size")
        limit = options.get("limit")

        if group_value:
            group = self._get_group(group_value)
            groups = [group]
        else:
            groups = list(LabelGroup.objects.filter(is_active=True))

        if not groups:
            self.stdout.write(self.style.WARNING("No active label groups found."))
            return

        report_ids = Report.objects.order_by("id").values_list("id", flat=True)
        if limit:
            report_ids = report_ids[:limit]
        report_ids = list(report_ids)

        if not report_ids:
            self.stdout.write(self.style.WARNING("No reports found."))
            return

        if batch_size is None:
            from django.conf import settings

            batch_size = settings.LABELING_TASK_BATCH_SIZE

        for group in groups:
            backfill_job = LabelBackfillJob.objects.create(
                label_group=group,
                status=LabelBackfillJob.Status.IN_PROGRESS,
                started_at=timezone.now(),
                total_reports=len(report_ids),
            )

            for report_batch in batched(report_ids, batch_size):
                process_label_group.defer(
                    label_group_id=group.id,
                    report_ids=list(report_batch),
                    backfill_job_id=backfill_job.id,
                )

            self.stdout.write(
                self.style.SUCCESS(
                    f"Enqueued labeling for {len(report_ids)} reports "
                    f"in group '{group.name}' (backfill job #{backfill_job.id})."
                )
            )

    def _get_group(self, value: str) -> LabelGroup:
        if value.isdigit():
            group = LabelGroup.objects.filter(id=int(value)).first()
        else:
            matches = LabelGroup.objects.filter(name=value)
            if matches.count() > 1:
                raise CommandError(
                    f"Multiple label groups named '{value}' exist. Use the numeric ID."
                )
            group = matches.first()

        if not group:
            raise CommandError(f"Label group '{value}' not found.")

        return group
