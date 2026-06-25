import logging
from itertools import batched

from django.conf import settings
from django.db import transaction
from django.db.models import F

from radis.chats.utils.chat_client import ChatClient
from radis.labels.throttled_client import ThrottledChatClient
from radis.reports.models import Report

from .models import GateAnswer, Label, LabelGroup, LabelResult
from .utils.prompts import render_gate_prompt, render_label_prompt
from .utils.schemas import (
    build_gate_schema,
    build_label_classification_schema,
)

logger = logging.getLogger(__name__)


def label_report(report_id: int) -> None:
    """Classify one report against all active label groups using the gate-then-label flow.

    The single function used by both execution paths. Nothing in its control flow
    branches on a label's bucket value — the LLM returns a bucket per label and it is stored
    as-is.
    """
    report = Report.objects.get(id=report_id)
    if not report.body or not report.body.strip():
        logger.warning("Report %s has empty body, skipping labeling.", report_id)
        return

    active_groups = list(
        LabelGroup.objects.filter(labels__active=True).prefetch_related("labels").distinct()
    )
    if not active_groups:
        logger.warning("No active label groups, skipping labeling of report %s.", report_id)
        return

    client = ThrottledChatClient(
        ChatClient(max_retries=0, timeout=settings.LLM_REQUEST_TIMEOUT_SECONDS)
    )

    existing_gates = {
        ga.label_group_id: ga
        for ga in GateAnswer.objects.filter(report=report, label_group__in=active_groups)
    }

    groups_needing_gate = [
        g
        for g in active_groups
        if g.id not in existing_gates or existing_gates[g.id].generated_at < g.updated_at
    ]
    needing_ids = {g.id for g in groups_needing_gate}
    groups_with_fresh_gate = {
        g.id: existing_gates[g.id].value for g in active_groups if g.id not in needing_ids
    }

    # Phase 1 — Gate: only for groups with stale or missing gate answers.
    new_gate_results: dict[int, str] = {}
    for gate_batch in batched(groups_needing_gate, settings.LABELING_GATE_BATCH_SIZE):
        schema = build_gate_schema(gate_batch)
        parsed = client.extract_data(render_gate_prompt(report.body), schema)
        result_map = parsed.model_dump()
        for g in gate_batch:
            new_gate_results[g.id] = str(result_map[g.name])

    # Phase 2 — process each group.
    for group in active_groups:
        labels = [lbl for lbl in group.labels.all() if lbl.active]

        if group.id in new_gate_results:
            new_value = new_gate_results[group.id]
            old_gate = existing_gates.get(group.id)
            old_value = old_gate.value if old_gate else None

            with transaction.atomic():
                GateAnswer.objects.update_or_create(
                    report=report, label_group=group, defaults={"value": new_value}
                )
                if new_value == GateAnswer.Value.NO and old_value == GateAnswer.Value.YES:
                    LabelResult.objects.filter(report=report, label__group=group).delete()

            if new_value == GateAnswer.Value.YES:
                labels_to_run = _get_stale_or_missing_labels(report, labels)
                if labels_to_run:
                    _run_label_set(client, report, labels_to_run)
        else:
            gate_value = groups_with_fresh_gate[group.id]
            if gate_value == GateAnswer.Value.YES:
                labels_to_run = _get_stale_or_missing_labels(report, labels)
                if labels_to_run:
                    _run_label_set(client, report, labels_to_run)
            # else: gate = NO, fresh — skip group entirely.


def _run_label_set(client: ThrottledChatClient, report: Report, labels: list[Label]) -> None:
    schema = build_label_classification_schema(labels)
    parsed = client.extract_data(render_label_prompt(report.body), schema)
    result_map = parsed.model_dump()
    for lbl in labels:
        LabelResult.objects.update_or_create(
            report=report, label_id=lbl.id, defaults={"value": result_map[lbl.name]}
        )


def _get_stale_or_missing_labels(report: Report, labels: list[Label]) -> list[Label]:
    """Return labels whose LabelResult is missing or stale (result.generated_at < label.updated_at).

    One query answers both "should we run?" (non-empty) and "what to run?" (the list).
    A label that previously came back ABSENT/UNMENTIONED still has a fresh row → excluded.
    """
    fresh_ids = set(
        LabelResult.objects.filter(
            report=report,
            label_id__in=[lbl.id for lbl in labels],
            generated_at__gte=F("label__updated_at"),
        ).values_list("label_id", flat=True)
    )
    return [lbl for lbl in labels if lbl.id not in fresh_ids]
