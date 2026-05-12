"""Compute side-by-side metrics for DIRECT vs REASONED labelling runs.

The harness reads ``Answer`` rows for the sample's reports, picks the
*latest* SUCCESS answer per (report, question, mode), and produces:

* Per-question agreement rate between modes.
* Mean confidence per mode and the delta on disagreements.
* The top-K highest-confidence disagreements with rationale and reasoning
  text so they can be inspected by hand.

The output is a dictionary built with stdlib types only so it can be
serialized to JSON, rendered in a template, or written to Markdown.
"""

from __future__ import annotations

from collections import defaultdict
from typing import TypedDict

from django.db.models import QuerySet

from ..models import Answer, EvalSample, LabelingRun


class DisagreementExample(TypedDict):
    report_id: int
    report_document_id: str
    question_label: str
    direct_choice: str
    direct_confidence: float | None
    direct_rationale: str
    reasoned_choice: str
    reasoned_confidence: float | None
    reasoned_rationale: str
    reasoning_text: str


class QuestionMetrics(TypedDict):
    label: str
    n_compared: int
    n_agree: int
    agreement_rate: float
    direct_mean_confidence: float | None
    reasoned_mean_confidence: float | None


def _latest_answers_for_mode(
    sample: EvalSample, mode: str
) -> QuerySet[Answer]:
    """Return the latest SUCCESS Answer per (report, question) for one mode.

    "Latest" is by ``run__created_at`` so re-runs after a version bump
    naturally win. A subquery would be cleaner but for our sample sizes
    a Python-side reduce is simpler and just as fast.
    """
    return (
        Answer.objects.filter(
            report__in=sample.reports.all(),
            run__question_set=sample.question_set,
            run__mode=mode,
            run__status=LabelingRun.Status.SUCCESS,
        )
        .select_related("option", "question", "run", "report")
        .order_by("-run__created_at")
    )


def _index_latest(answers: QuerySet[Answer]) -> dict[tuple[int, int], Answer]:
    """First-seen wins because the queryset is ordered newest-first."""
    out: dict[tuple[int, int], Answer] = {}
    for answer in answers:
        key = (answer.report_id, answer.question_id)
        if key not in out:
            out[key] = answer
    return out


def compute_eval(sample: EvalSample, top_disagreements: int = 20) -> dict:
    """Compare DIRECT vs REASONED answers for every (report, question) pair
    that has both modes complete in this sample.
    """
    active_questions = list(
        sample.question_set.questions.filter(is_active=True).order_by("order", "label")
    )

    direct_index = _index_latest(_latest_answers_for_mode(sample, LabelingRun.Mode.DIRECT))
    reasoned_index = _index_latest(
        _latest_answers_for_mode(sample, LabelingRun.Mode.REASONED)
    )

    per_question_acc: dict[int, dict[str, float | int]] = defaultdict(
        lambda: {
            "n_compared": 0,
            "n_agree": 0,
            "direct_conf_sum": 0.0,
            "direct_conf_count": 0,
            "reasoned_conf_sum": 0.0,
            "reasoned_conf_count": 0,
        }
    )

    disagreements: list[DisagreementExample] = []

    for question in active_questions:
        for report_id in sample.reports.values_list("id", flat=True):
            key = (report_id, question.id)
            direct = direct_index.get(key)
            reasoned = reasoned_index.get(key)
            if direct is None or reasoned is None:
                continue

            acc = per_question_acc[question.id]
            acc["n_compared"] = int(acc["n_compared"]) + 1
            if direct.option_id == reasoned.option_id:
                acc["n_agree"] = int(acc["n_agree"]) + 1
            else:
                disagreements.append(
                    {
                        "report_id": report_id,
                        "report_document_id": direct.report.document_id,
                        "question_label": question.label,
                        "direct_choice": direct.option.label,
                        "direct_confidence": direct.confidence,
                        "direct_rationale": direct.rationale,
                        "reasoned_choice": reasoned.option.label,
                        "reasoned_confidence": reasoned.confidence,
                        "reasoned_rationale": reasoned.rationale,
                        "reasoning_text": reasoned.run.reasoning_text,
                    }
                )

            if direct.confidence is not None:
                acc["direct_conf_sum"] = float(acc["direct_conf_sum"]) + direct.confidence
                acc["direct_conf_count"] = int(acc["direct_conf_count"]) + 1
            if reasoned.confidence is not None:
                acc["reasoned_conf_sum"] = (
                    float(acc["reasoned_conf_sum"]) + reasoned.confidence
                )
                acc["reasoned_conf_count"] = int(acc["reasoned_conf_count"]) + 1

    per_question: list[QuestionMetrics] = []
    for question in active_questions:
        acc = per_question_acc.get(question.id)
        if acc is None or acc["n_compared"] == 0:
            per_question.append(
                {
                    "label": question.label,
                    "n_compared": 0,
                    "n_agree": 0,
                    "agreement_rate": 0.0,
                    "direct_mean_confidence": None,
                    "reasoned_mean_confidence": None,
                }
            )
            continue
        n_compared = int(acc["n_compared"])
        n_agree = int(acc["n_agree"])
        per_question.append(
            {
                "label": question.label,
                "n_compared": n_compared,
                "n_agree": n_agree,
                "agreement_rate": n_agree / n_compared,
                "direct_mean_confidence": _safe_mean(
                    float(acc["direct_conf_sum"]), int(acc["direct_conf_count"])
                ),
                "reasoned_mean_confidence": _safe_mean(
                    float(acc["reasoned_conf_sum"]), int(acc["reasoned_conf_count"])
                ),
            }
        )

    # Highest-confidence disagreements first — those are the most consequential
    # divergences. We approximate "confidence" of a disagreement as the max of
    # the two confidences so a confident-no vs confident-yes lands above a
    # low-confidence flip.
    def _conf_of(item: DisagreementExample) -> float:
        return max(item.get("direct_confidence") or 0.0, item.get("reasoned_confidence") or 0.0)

    disagreements.sort(key=_conf_of, reverse=True)

    overall_compared = sum(int(acc["n_compared"]) for acc in per_question_acc.values())
    overall_agree = sum(int(acc["n_agree"]) for acc in per_question_acc.values())

    return {
        "sample_name": sample.name,
        "question_set_name": sample.question_set.name,
        "sample_size_pinned": sample.actual_size,
        "overall": {
            "n_compared": overall_compared,
            "n_agree": overall_agree,
            "agreement_rate": (overall_agree / overall_compared) if overall_compared else 0.0,
        },
        "per_question": per_question,
        "disagreements": disagreements[:top_disagreements],
    }


def _safe_mean(total: float, count: int) -> float | None:
    if count == 0:
        return None
    return total / count


def render_markdown(report: dict) -> str:
    """Render the dict from :func:`compute_eval` as Markdown.

    Kept on the metrics module so callers (CLI command, view) share one
    representation and don't drift in formatting choices.
    """
    lines: list[str] = []
    lines.append(f"# Evaluation report — {report['question_set_name']}")
    lines.append("")
    lines.append(f"Sample: **{report['sample_name']}** ({report['sample_size_pinned']} reports)")
    lines.append("")

    overall = report["overall"]
    if overall["n_compared"]:
        lines.append(
            f"**Overall agreement (DIRECT vs REASONED):** "
            f"{overall['n_agree']} / {overall['n_compared']} = "
            f"{overall['agreement_rate']:.1%}"
        )
    else:
        lines.append("**No comparable (DIRECT + REASONED) answers yet.**")
    lines.append("")

    lines.append("## Per-question")
    lines.append("")
    header = (
        "| Question | Compared | Agree | Agreement "
        "| Mean conf. DIRECT | Mean conf. REASONED |"
    )
    lines.append(header)
    lines.append("|---|---:|---:|---:|---:|---:|")
    for row in report["per_question"]:
        direct_conf = (
            f"{row['direct_mean_confidence']:.2f}"
            if row["direct_mean_confidence"] is not None
            else "—"
        )
        reasoned_conf = (
            f"{row['reasoned_mean_confidence']:.2f}"
            if row["reasoned_mean_confidence"] is not None
            else "—"
        )
        agreement = (
            f"{row['agreement_rate']:.1%}" if row["n_compared"] else "—"
        )
        lines.append(
            f"| {row['label']} | {row['n_compared']} | {row['n_agree']} "
            f"| {agreement} | {direct_conf} | {reasoned_conf} |"
        )
    lines.append("")

    lines.append("## Top disagreements")
    lines.append("")
    if not report["disagreements"]:
        lines.append("_No disagreements yet._")
    else:
        for item in report["disagreements"]:
            lines.append(
                f"### {item['report_document_id']} — {item['question_label']}"
            )
            lines.append("")
            lines.append(
                f"- **DIRECT**: {item['direct_choice']} "
                f"(conf {item['direct_confidence']})"
            )
            if item["direct_rationale"]:
                lines.append(f"  - _{item['direct_rationale']}_")
            lines.append(
                f"- **REASONED**: {item['reasoned_choice']} "
                f"(conf {item['reasoned_confidence']})"
            )
            if item["reasoned_rationale"]:
                lines.append(f"  - _{item['reasoned_rationale']}_")
            if item["reasoning_text"]:
                lines.append("")
                lines.append("**Reasoning step output:**")
                lines.append("")
                lines.append(f"> {item['reasoning_text']}")
            lines.append("")

    return "\n".join(lines)
