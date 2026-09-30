import time
from collections.abc import Sequence
from dataclasses import dataclass
from itertools import batched

from django.conf import settings
from pydantic import BaseModel

from radis.core.utils.llm_client import LLMClient
from radis.labels.utils.prompts import render_gate_prompt, render_label_prompt
from radis.labels.utils.schemas import build_gate_schema, build_label_classification_schema

from .questions import GroupSpec


@dataclass(frozen=True)
class LLMCall:
    purpose: str
    prompt: str
    schema: dict
    output: dict
    elapsed_ms: float


@dataclass(frozen=True)
class Baseline:
    model: str
    gates: dict[int, str]
    buckets: dict[int, str]
    calls: tuple[LLMCall, ...]
    elapsed_ms: float


def run_llm_baseline(report_body: str, groups: Sequence[GroupSpec]) -> Baseline:
    """What the labeling pipeline's LLM says about a report, without storing anything.

    Sends the prompts and schemas `label_report` sends. Unlike the pipeline it classifies
    the labels of every group, also behind a NO gate, so that the decision model's answers
    always have an LLM answer next to them.
    """
    client = LLMClient("labeling")
    calls: list[LLMCall] = []
    started = time.perf_counter()

    gates: dict[int, str] = {}
    for gate_batch in batched(groups, settings.LABELING_GATE_BATCH_SIZE):
        answers = _ask(
            client, calls, "gate", render_gate_prompt(report_body), build_gate_schema(gate_batch)
        )
        for group in gate_batch:
            gates[group.id] = answers[group.name]

    buckets: dict[int, str] = {}
    for group in groups:
        if not group.labels:
            continue
        answers = _ask(
            client,
            calls,
            f"labels: {group.name}",
            render_label_prompt(report_body),
            build_label_classification_schema(group.labels),
        )
        for label in group.labels:
            buckets[label.id] = answers[label.name]

    return Baseline(
        model=settings.LLM_MODELS["labeling"].model,
        gates=gates,
        buckets=buckets,
        calls=tuple(calls),
        elapsed_ms=(time.perf_counter() - started) * 1000,
    )


def _ask(
    client: LLMClient, calls: list[LLMCall], purpose: str, prompt: str, schema: type[BaseModel]
) -> dict:
    started = time.perf_counter()
    output = client.extract_data(prompt, schema).model_dump(mode="json")
    calls.append(
        LLMCall(
            purpose=purpose,
            prompt=prompt,
            schema=schema.model_json_schema(),
            output=output,
            elapsed_ms=(time.perf_counter() - started) * 1000,
        )
    )
    return output
