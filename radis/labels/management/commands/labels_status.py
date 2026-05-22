from django.core.management.base import BaseCommand
from django.db.models import Count, F, Q

from radis.labels.models import Question
from radis.reports.models import Report


class Command(BaseCommand):
    help = "Print labeling coverage for the corpus."

    def handle(self, *args, **opts):
        total = Report.objects.count()
        active_q = Question.objects.filter(active=True).count()
        self.stdout.write(f"Total reports: {total}")
        self.stdout.write(f"Active questions: {active_q}")
        if active_q == 0:
            self.stdout.write("No active questions — nothing to report.")
            return

        fully_current = (
            Report.objects.annotate(
                non_stale_count=Count(
                    "answers",
                    filter=Q(
                        answers__question__active=True,
                        answers__generated_at__gte=F(
                            "answers__question__updated_at"
                        ),
                    ),
                )
            )
            .filter(non_stale_count=active_q)
            .count()
        )
        self.stdout.write(f"Fully current: {fully_current}")
        self.stdout.write(f"Missing or stale: {total - fully_current}")

        for q in Question.objects.filter(active=True).order_by("group", "label"):
            counts = q.answers.aggregate(
                yes=Count("pk", filter=Q(value="YES")),
                no=Count("pk", filter=Q(value="NO")),
                maybe=Count("pk", filter=Q(value="MAYBE")),
                stale=Count("pk", filter=Q(generated_at__lt=q.updated_at)),
            )
            self.stdout.write(
                f"  [{q.group}] {q.label}: {counts['yes']} Y · "
                f"{counts['maybe']} M · {counts['no']} N · {counts['stale']} stale"
            )
