from django.db.models import Count, Exists, F, OuterRef, Q, QuerySet

from radis.reports.models import Report

from .models import GateAnswer, Label


def _needs_work_queryset(active_group_count: int) -> QuerySet:
    """Reports needing labeling work: missing/stale gate (condition A) OR a fresh YES/MAYBE
    group with a missing/stale label result (condition B)."""
    return Report.objects.annotate(
        non_stale_gate_count=Count(
            "gate_answers",
            filter=Q(
                gate_answers__label_group__labels__active=True,
                gate_answers__generated_at__gte=F("gate_answers__label_group__updated_at"),
            ),
            distinct=True,
        ),
    ).filter(
        Q(non_stale_gate_count__lt=active_group_count)
        | Exists(
            GateAnswer.objects.filter(
                report=OuterRef("pk"),
                value__in=[GateAnswer.Value.YES, GateAnswer.Value.MAYBE],
                generated_at__gte=F("label_group__updated_at"),
            ).filter(
                Exists(
                    Label.objects.filter(
                        group_id=OuterRef("label_group_id"),
                        active=True,
                    ).exclude(
                        results__report_id=OuterRef(OuterRef("pk")),
                        results__generated_at__gte=F("updated_at"),
                    )
                )
            )
        )
    )
