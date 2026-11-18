from django.db.models import Exists, F, OuterRef, QuerySet

from radis.reports.models import Report

from .models import GateAnswer, Label, LabelGroup, LabelResult


def _needs_work_queryset() -> QuerySet:
    """Reports with a missing/stale gate for an active group, or a missing/stale result
    for an active label behind a fresh YES gate. Correlated EXISTS only, no aggregates."""
    fresh_gate_for_group = GateAnswer.objects.filter(
        report_id=OuterRef(OuterRef("pk")),
        label_group_id=OuterRef("pk"),
        generated_at__gte=OuterRef("updated_at"),
    ).filter(generated_at__gte=OuterRef(OuterRef("updated_at")))
    active_group_without_fresh_gate = LabelGroup.objects.filter(
        Exists(Label.objects.filter(group_id=OuterRef("pk"), active=True))
    ).filter(~Exists(fresh_gate_for_group))

    # Both predicates must match the SAME LabelResult row — otherwise a fresh result on
    # another report could mask this report's stale one.
    fresh_result_for_report = LabelResult.objects.filter(
        report_id=OuterRef(OuterRef("pk")),
        label_id=OuterRef("pk"),
        generated_at__gte=OuterRef("updated_at"),
    ).filter(generated_at__gte=OuterRef(OuterRef("updated_at")))
    fresh_yes_gate = GateAnswer.objects.filter(
        report_id=OuterRef(OuterRef("pk")),
        label_group_id=OuterRef("group_id"),
        value=GateAnswer.Value.YES,
        generated_at__gte=F("label_group__updated_at"),
    ).filter(generated_at__gte=OuterRef(OuterRef("updated_at")))
    active_label_without_fresh_result = (
        Label.objects.filter(active=True)
        .filter(Exists(fresh_yes_gate))
        .filter(~Exists(fresh_result_for_report))
    )

    return Report.objects.filter(
        Exists(active_group_without_fresh_gate) | Exists(active_label_without_fresh_result)
    )
