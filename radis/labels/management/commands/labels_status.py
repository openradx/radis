from typing import Any

from django.core.management.base import BaseCommand
from django.db.models import F

from radis.labels.models import (
    GateAnswer,
    Label,
    LabelGroup,
    LabelingScanCheckpoint,
    LabelResult,
)
from radis.reports.models import Report


class Command(BaseCommand):
    help = "Report corpus-wide auto-labeling status."

    def handle(self, *args: Any, **options: Any) -> None:
        checkpoint = LabelingScanCheckpoint.objects.filter(pk=1).first()
        last = checkpoint.last_scanned_at if checkpoint and checkpoint.last_scanned_at else "never"
        self.stdout.write(f"Last scan checkpoint: {last}")
        self.stdout.write(f"Total reports: {Report.objects.count()}")

        self.stdout.write("\nPer-label results:")
        for label in Label.objects.select_related("group").order_by("group__name", "name"):
            counts = {v.label: label.results.filter(value=v).count() for v in LabelResult.Value}
            stale = label.results.filter(generated_at__lt=F("label__updated_at")).count()
            summary = " · ".join(f"{n} {lbl}" for lbl, n in counts.items())
            self.stdout.write(f"  [{label.group.name}] {label.name}: {summary} · {stale} stale")

        self.stdout.write("\nPer-group gate answers:")
        for group in LabelGroup.objects.order_by("name"):
            gc = {v.label: group.gate_answers.filter(value=v).count() for v in GateAnswer.Value}
            gstale = group.gate_answers.filter(
                generated_at__lt=F("label_group__updated_at")
            ).count()
            summary = " · ".join(f"{n} {lbl}" for lbl, n in gc.items())
            self.stdout.write(f"  {group.name}: {summary} · {gstale} stale")
