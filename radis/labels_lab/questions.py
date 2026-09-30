"""The labeling decisions phrased as decision-model questions, and the way back.

All questions about a report travel in one request. Their ids tie each answer to the group
or label it belongs to:

    gate_noul:<group id>     the gate question as a yes/no probability
    gate_choice:<group id>   the same question as a two-option choice
    bucket:<label id>        the label as a choice over the five buckets
    addressed:<label id>     the label as two yes/no probabilities, which
    present:<label id>       bucket_from_nouls turns into a bucket
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import TypeGuard

from radis.labels.utils.schemas import BucketValue, GateValue

# The bucket definitions of the default labeling prompt (LABELING_SYSTEM_PROMPT), so the
# decision model is asked in the words the LLM is.
BUCKET_CRITERIA = {
    "PRESENT": "the report clearly states this is present",
    "LIKELY": "the report strongly suggests it, without stating it outright",
    "POSSIBLE": "the report leaves it as a possibility / cannot be excluded",
    "ABSENT": "the report explicitly states this is not present",
    "UNMENTIONED": "the report does not address this at all",
}

# Laya renders a noul's two options as "true:" / "false:", and on its English checkpoint
# that pair can outweigh the report. Its model card recommends asking such a question as a
# choice with neutral keys instead, so the gate is asked both ways.
GATE_CHOICE_YES = "A"
GATE_CHOICE_CRITERIA = {GATE_CHOICE_YES: "yes", "B": "no"}


@dataclass(frozen=True)
class LabelSpec:
    id: int
    name: str
    description: str


@dataclass(frozen=True)
class GroupSpec:
    id: int
    name: str
    gate_question: str
    labels: tuple[LabelSpec, ...]


@dataclass(frozen=True)
class Thresholds:
    """Where probabilities turn into labeling values. A value at a threshold counts as met."""

    gate: float = 0.5  # P(yes) for a gate to answer YES
    addressed: float = 0.5  # P(addressed) for a label to be anything but UNMENTIONED
    possible: float = 0.35  # P(present) for POSSIBLE, below it the label is ABSENT
    likely: float = 0.6  # P(present) for LIKELY
    present: float = 0.85  # P(present) for PRESENT


@dataclass(frozen=True)
class GateView:
    llm: str | None
    noul: float | None
    noul_value: str | None
    choice_yes: float | None
    choice_value: str | None


@dataclass(frozen=True)
class LabelView:
    spec: LabelSpec
    llm: str | None
    choice: str | None
    choice_confidence: float | None
    choice_probabilities: dict[str, float]
    addressed: float | None
    present: float | None
    nouls_value: str | None
    choice_differs: bool
    nouls_differs: bool


@dataclass(frozen=True)
class GroupView:
    spec: GroupSpec
    gate: GateView
    labels: tuple[LabelView, ...]

    # The labeling pipeline classifies no label of a group whose gate answered NO. Each
    # way of asking has its own gate answer, so each decides that for itself.
    @property
    def llm_skipped(self) -> bool:
        return self.gate.llm == GateValue.NO

    @property
    def choice_skipped(self) -> bool:
        return self.gate.choice_value == GateValue.NO

    @property
    def nouls_skipped(self) -> bool:
        return self.gate.noul_value == GateValue.NO


def build_questions(groups: Iterable[GroupSpec]) -> dict[str, dict]:
    questions: dict[str, dict] = {}
    for group in groups:
        questions[f"gate_noul:{group.id}"] = {
            "type": "noul",
            "instructions": group.gate_question,
        }
        questions[f"gate_choice:{group.id}"] = {
            "type": "choice",
            "instructions": group.gate_question,
            "criteria": dict(GATE_CHOICE_CRITERIA),
        }
        for label in group.labels:
            finding = f'the finding "{label.name}"'
            definition = f"Definition: {label.description}"
            questions[f"bucket:{label.id}"] = {
                "type": "choice",
                "instructions": f"How strongly does the report support {finding}? {definition}",
                "criteria": dict(BUCKET_CRITERIA),
            }
            questions[f"addressed:{label.id}"] = {
                "type": "noul",
                "instructions": (
                    f"Does the report address {finding} at all, whether as present or as "
                    f"absent? {definition}"
                ),
            }
            questions[f"present:{label.id}"] = {
                "type": "noul",
                "instructions": (
                    f"Does the report state or suggest that {finding} is present? {definition}"
                ),
            }
    return questions


def bucket_from_nouls(addressed: float, present: float, thresholds: Thresholds) -> str:
    if addressed < thresholds.addressed:
        return BucketValue.UNMENTIONED
    if present >= thresholds.present:
        return BucketValue.PRESENT
    if present >= thresholds.likely:
        return BucketValue.LIKELY
    if present >= thresholds.possible:
        return BucketValue.POSSIBLE
    return BucketValue.ABSENT


def map_answers(
    groups: Iterable[GroupSpec],
    answers: Mapping[str, object],
    thresholds: Thresholds,
    llm_gates: Mapping[int, str] | None = None,
    llm_buckets: Mapping[int, str] | None = None,
) -> list[GroupView]:
    """Line up the decision model's answers with the LLM's results, group by group.

    An answer that is missing or not of the expected shape leaves its part of the view
    empty, so a failed or partial run still shows whatever did come back.
    """
    llm_gates = llm_gates or {}
    llm_buckets = llm_buckets or {}

    views = []
    for group in groups:
        noul = _number(answers.get(f"gate_noul:{group.id}"), "noul")
        choice_yes = _number(
            _mapping(answers.get(f"gate_choice:{group.id}")).get("probabilities"),
            GATE_CHOICE_YES,
        )
        gate = GateView(
            llm=llm_gates.get(group.id),
            noul=noul,
            noul_value=_gate_value(noul, thresholds),
            choice_yes=choice_yes,
            choice_value=_gate_value(choice_yes, thresholds),
        )
        labels = tuple(
            _label_view(label, answers, thresholds, llm_buckets.get(label.id))
            for label in group.labels
        )
        views.append(GroupView(spec=group, gate=gate, labels=labels))
    return views


def _label_view(
    label: LabelSpec, answers: Mapping[str, object], thresholds: Thresholds, llm: str | None
) -> LabelView:
    bucket = _mapping(answers.get(f"bucket:{label.id}"))
    choice = bucket.get("choice")
    choice = choice if isinstance(choice, str) else None
    probabilities = {
        str(option): float(probability)
        for option, probability in _mapping(bucket.get("probabilities")).items()
        if _is_number(probability)
    }

    addressed = _number(answers.get(f"addressed:{label.id}"), "noul")
    present = _number(answers.get(f"present:{label.id}"), "noul")
    nouls_value = (
        bucket_from_nouls(addressed, present, thresholds)
        if addressed is not None and present is not None
        else None
    )

    return LabelView(
        spec=label,
        llm=llm,
        choice=choice,
        choice_confidence=_number(bucket, "confidence"),
        choice_probabilities=probabilities,
        addressed=addressed,
        present=present,
        nouls_value=nouls_value,
        choice_differs=None not in (llm, choice) and choice != llm,
        nouls_differs=None not in (llm, nouls_value) and nouls_value != llm,
    )


def _gate_value(probability: float | None, thresholds: Thresholds) -> str | None:
    if probability is None:
        return None
    return GateValue.YES if probability >= thresholds.gate else GateValue.NO


def _mapping(value: object) -> Mapping:
    return value if isinstance(value, Mapping) else {}


def _is_number(value: object) -> TypeGuard[int | float]:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _number(container: object, key: str) -> float | None:
    value = _mapping(container).get(key)
    return float(value) if _is_number(value) else None
