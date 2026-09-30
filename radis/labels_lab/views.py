import json
import logging
from collections.abc import Mapping
from dataclasses import asdict
from typing import Any

from django.conf import settings
from django.contrib.admin.views.decorators import staff_member_required
from django.db.models import QuerySet
from django.http import HttpRequest, HttpResponse, HttpResponseBadRequest
from django.shortcuts import render
from django.views.decorators.http import require_GET, require_POST
from django_htmx.http import reswap, retarget

from radis.labels.models import LabelGroup
from radis.reports.models import Report

from .baseline import Baseline, run_llm_baseline
from .decision_client import Decision, DecisionClient, DecisionModelError
from .forms import LabForm, ThresholdsForm, groups_with_active_labels
from .questions import GroupSpec, LabelSpec, Thresholds, build_questions, map_answers
from .samples import LANGUAGES, random_sample_report

logger = logging.getLogger(__name__)


@staff_member_required
def lab_view(request: HttpRequest) -> HttpResponse:
    models, models_error = _offered_models()
    return render(
        request,
        "labels_lab/lab.html",
        {
            "groups": groups_with_active_labels(),
            "models": models,
            "models_error": models_error,
            "default_model": settings.DECISION_MODEL,
            "decision_model_url": settings.DECISION_MODEL_URL,
            "llm_model": settings.LLM_MODELS["labeling"].model,
            "thresholds": Thresholds(),
            "info": "Paste a report, or load one with the buttons below.",
        },
    )


@staff_member_required
@require_GET
def lab_text_view(request: HttpRequest) -> HttpResponse:
    source = request.GET.get("source", "")
    if source in LANGUAGES:
        text = random_sample_report(source)
        info = f"Random {source.upper()} sample report, {len(text)} characters."
    elif source == "report":
        reference = request.GET.get("report", "").strip()
        report = _find_report(reference)
        if report is None:
            # Only the info line is replaced, so whatever is in the text box stays there.
            response = render(
                request,
                "labels_lab/_text_info.html",
                {"info": f'No report has the ID or document ID "{reference}".', "error": True},
            )
            return reswap(retarget(response, "#lab-text-info"), "innerHTML")
        text = report.body
        info = (
            f"Report {report.pk} (document ID {report.document_id}, "
            f"language {report.language.code}), {len(text)} characters."
        )
    else:
        return HttpResponseBadRequest("Unknown text source.")

    return render(request, "labels_lab/_text.html", {"text": text, "info": info})


@staff_member_required
@require_POST
def lab_run_view(request: HttpRequest) -> HttpResponse:
    form = LabForm(request.POST)
    if not form.is_valid():
        return render(request, "labels_lab/_results.html", {"errors": _form_errors(form)})

    text = form.cleaned_data["text"]
    groups = _group_specs(form.cleaned_data["groups"])

    decision: Decision | None = None
    decision_error: DecisionModelError | None = None
    try:
        decision = DecisionClient().decide(
            text,
            build_questions(groups),
            model=form.cleaned_data["model"],
            extras=["laya"] if form.cleaned_data["extras"] else None,
        )
    except DecisionModelError as err:
        logger.warning("Label lab: the decision model did not answer: %s", err)
        decision_error = err

    baseline: Baseline | None = None
    baseline_error: str | None = None
    if form.cleaned_data["run_baseline"]:
        try:
            baseline = run_llm_baseline(text, groups)
        except Exception as err:
            # Whatever breaks on the LLM side is shown next to the decision model's
            # answers instead of taking the whole run down with it.
            logger.exception("Label lab: the LLM baseline failed")
            baseline_error = f"{type(err).__name__}: {err}"

    answers = decision.response["answers"] if decision else {}
    llm_gates = baseline.gates if baseline else {}
    llm_buckets = baseline.buckets if baseline else {}
    thresholds = form.thresholds()

    return render(
        request,
        "labels_lab/_results.html",
        {
            "group_views": map_answers(groups, answers, thresholds, llm_gates, llm_buckets),
            "thresholds": thresholds,
            # What lab_remap_view needs to map these answers again under other thresholds.
            "mapping_json": json.dumps(
                {
                    "groups": [asdict(group) for group in groups],
                    "answers": answers,
                    "llm_gates": llm_gates,
                    "llm_buckets": llm_buckets,
                }
            ),
            "decision": _decision_context(decision) if decision else None,
            "decision_error": decision_error,
            "baseline": _baseline_context(baseline) if baseline else None,
            "baseline_error": baseline_error,
        },
    )


@staff_member_required
@require_POST
def lab_remap_view(request: HttpRequest) -> HttpResponse:
    """Apply changed thresholds to the answers of the last run, without asking any model."""
    raw_mapping = request.POST.get("mapping")
    if raw_mapping is None:
        return HttpResponse(status=204)

    try:
        groups, answers, llm_gates, llm_buckets = _load_mapping(raw_mapping)
    except (ValueError, TypeError, KeyError, AttributeError, OverflowError, RecursionError):
        return HttpResponseBadRequest("The mapping data of the last run is unreadable.")

    form = ThresholdsForm(request.POST)
    if not form.is_valid():
        return render(request, "labels_lab/_mapped.html", {"errors": _form_errors(form)})

    thresholds = form.thresholds()
    return render(
        request,
        "labels_lab/_mapped.html",
        {
            "group_views": map_answers(groups, answers, thresholds, llm_gates, llm_buckets),
            "thresholds": thresholds,
        },
    )


def _offered_models() -> tuple[list[str], str | None]:
    """The models to choose from, and why the endpoint could not be asked if it could not."""
    try:
        models = DecisionClient().list_models()
    except DecisionModelError as err:
        logger.warning("Label lab: could not list the decision models: %s", err)
        return [settings.DECISION_MODEL], str(err)
    if settings.DECISION_MODEL not in models:
        models.insert(0, settings.DECISION_MODEL)
    return models, None


def _find_report(reference: str) -> Report | None:
    reports = Report.objects.select_related("language")
    # Longer digit strings cannot be a primary key and would overflow the lookup.
    if reference.isdecimal() and len(reference) <= 18:
        report = reports.filter(pk=int(reference)).first()
        if report is not None:
            return report
    return reports.filter(document_id=reference).first()


def _group_specs(groups: QuerySet[LabelGroup]) -> list[GroupSpec]:
    """The groups with their active labels, read the way `label_report` reads them.

    The labels keep whatever order that query returns them in. The LLM fills the fields of
    its schema in order and its answers depend on it, so sorting the labels here would turn
    the baseline into a call the pipeline never makes.
    """
    return [
        GroupSpec(
            id=group.id,
            name=group.name,
            gate_question=group.gate_question,
            labels=tuple(
                LabelSpec(id=label.id, name=label.name, description=label.description)
                for label in group.labels.all()
                if label.active
            ),
        )
        for group in groups.prefetch_related("labels")
    ]


def _load_mapping(
    raw: str,
) -> tuple[list[GroupSpec], Mapping[str, object], dict[int, str], dict[int, str]]:
    """Read back what lab_run_view put into the page for lab_remap_view."""
    data = json.loads(raw)
    groups = [
        GroupSpec(
            id=int(group["id"]),
            name=str(group["name"]),
            gate_question=str(group["gate_question"]),
            labels=tuple(
                LabelSpec(
                    id=int(label["id"]),
                    name=str(label["name"]),
                    description=str(label["description"]),
                )
                for label in group["labels"]
            ),
        )
        for group in data["groups"]
    ]
    answers = data["answers"]
    if not isinstance(answers, dict):
        raise TypeError("answers must be an object")
    llm_gates = {int(group_id): str(value) for group_id, value in data["llm_gates"].items()}
    llm_buckets = {int(label_id): str(value) for label_id, value in data["llm_buckets"].items()}
    return groups, answers, llm_gates, llm_buckets


def _form_errors(form: ThresholdsForm) -> list[str]:
    errors = []
    for field, messages in form.errors.items():
        prefix = "" if field == "__all__" else f"{field}: "
        errors.extend(f"{prefix}{message}" for message in messages)
    return errors


def _decision_context(decision: Decision) -> dict[str, Any]:
    response = decision.response
    usage = response.get("usage")
    return {
        "model": response.get("model"),
        "routing": response.get("routing"),
        "input_tokens": usage.get("input_tokens") if isinstance(usage, dict) else None,
        "truncated": response.get("state_truncated") is True,
        "eval_ms": _milliseconds(response.get("eval_duration")),
        "load_ms": _milliseconds(response.get("load_duration")),
        "elapsed_ms": decision.elapsed_ms,
        "question_count": len(decision.request["questions"]),
        "request_json": _pretty(decision.request),
        "response_json": _pretty(response),
    }


def _baseline_context(baseline: Baseline) -> dict[str, Any]:
    return {
        "model": baseline.model,
        "elapsed_ms": baseline.elapsed_ms,
        "calls": [
            {
                "purpose": call.purpose,
                "elapsed_ms": call.elapsed_ms,
                "prompt": call.prompt,
                "schema_json": _pretty(call.schema),
                "output_json": _pretty(call.output),
            }
            for call in baseline.calls
        ],
    }


def _milliseconds(nanoseconds: object) -> float | None:
    """Ollaya reports its native durations in nanoseconds; other endpoints have none."""
    if isinstance(nanoseconds, (int, float)) and not isinstance(nanoseconds, bool):
        return nanoseconds / 1_000_000
    return None


def _pretty(data: object) -> str:
    return json.dumps(data, indent=2, ensure_ascii=False)
