from django.db.models import Count, Exists, F, OuterRef, Q, QuerySet

from radis.reports.models import Report

from .models import GateAnswer, Label, LabelResult


def _needs_work_queryset(active_group_count: int) -> QuerySet:
    """Reports needing labeling work: missing/stale gate (condition A) OR a fresh YES
    group with a missing/stale label result (condition B)."""
    # Both predicates must match the SAME LabelResult row — otherwise a fresh result on
    # another report could mask this report's stale one.
    fresh_result_for_report = LabelResult.objects.filter(
        report_id=OuterRef(OuterRef("pk")),
        label_id=OuterRef("pk"),
        generated_at__gte=OuterRef("updated_at"),
    )
    fresh_yes_gate = GateAnswer.objects.filter(
        report_id=OuterRef(OuterRef("pk")),
        label_group_id=OuterRef("group_id"),
        value=GateAnswer.Value.YES,
        generated_at__gte=F("label_group__updated_at"),
    )
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
            Label.objects.filter(active=True)
            .filter(Exists(fresh_yes_gate))
            .filter(~Exists(fresh_result_for_report))
        )
    )
