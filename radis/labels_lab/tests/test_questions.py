import pytest

from radis.labels.models import LabelResult
from radis.labels_lab.questions import (
    NOT_SURFACED,
    GroupSpec,
    LabelSpec,
    Thresholds,
    build_questions,
    map_answers,
    outcome_from_noul,
)

CHEST = GroupSpec(
    id=7,
    name="Chest pathology",
    gate_question="Does this report describe imaging of the chest?",
    labels=(
        LabelSpec(id=3, name="pneumonia", description="Infectious consolidation of the lung."),
        LabelSpec(id=4, name="pneumothorax", description="Air in the pleural space."),
    ),
)

# The "answers" object of an Ollaya /api/decide response for the questions CHEST produces.
CHEST_ANSWERS = {
    "gate_noul:7": {"type": "noul", "noul": 0.9127},
    "gate_choice:7": {
        "type": "choice",
        "choice": "A",
        "confidence": 0.7,
        "probabilities": {"A": 0.85, "B": 0.15},
    },
    "bucket:3": {
        "type": "choice",
        "choice": "LIKELY",
        "confidence": 0.5614,
        "probabilities": {
            "PRESENT": 0.2,
            "LIKELY": 0.6491,
            "POSSIBLE": 0.1,
            "ABSENT": 0.03,
            "UNMENTIONED": 0.0209,
        },
    },
    "present:3": {"type": "noul", "noul": 0.7},
    "bucket:4": {
        "type": "choice",
        "choice": "ABSENT",
        "confidence": 0.75,
        "probabilities": {
            "PRESENT": 0.05,
            "LIKELY": 0.05,
            "POSSIBLE": 0.05,
            "ABSENT": 0.8,
            "UNMENTIONED": 0.05,
        },
    },
    "present:4": {"type": "noul", "noul": 0.05},
}


def test_question_ids_cover_every_group_and_label():
    assert set(build_questions([CHEST])) == {
        "gate_noul:7",
        "gate_choice:7",
        "bucket:3",
        "present:3",
        "bucket:4",
        "present:4",
    }


def test_gate_is_asked_as_noul_with_the_gate_question_verbatim():
    assert build_questions([CHEST])["gate_noul:7"] == {
        "type": "noul",
        "instructions": "Does this report describe imaging of the chest?",
    }


def test_gate_is_also_asked_as_choice_with_neutral_option_keys():
    assert build_questions([CHEST])["gate_choice:7"] == {
        "type": "choice",
        "instructions": "Does this report describe imaging of the chest?",
        "criteria": {"A": "yes", "B": "no"},
    }


def test_label_is_asked_as_choice_over_the_five_buckets():
    question = build_questions([CHEST])["bucket:3"]

    assert question["type"] == "choice"
    assert list(question["criteria"]) == ["PRESENT", "LIKELY", "POSSIBLE", "ABSENT", "UNMENTIONED"]
    assert "pneumonia" in question["instructions"]
    assert "Infectious consolidation of the lung." in question["instructions"]


def test_the_choice_offers_exactly_the_buckets_the_labeling_pipeline_stores():
    assert list(build_questions([CHEST])["bucket:3"]["criteria"]) == LabelResult.Value.values


def test_label_is_also_asked_as_a_present_noul():
    question = build_questions([CHEST])["present:4"]

    assert question["type"] == "noul"
    assert "pneumothorax" in question["instructions"]
    assert "Air in the pleural space." in question["instructions"]


@pytest.mark.parametrize(
    "present, want",
    [
        (0.0, NOT_SURFACED),
        (0.34, NOT_SURFACED),
        (0.35, "POSSIBLE"),
        (0.59, "POSSIBLE"),
        (0.6, "LIKELY"),
        (0.84, "LIKELY"),
        (0.85, "PRESENT"),
        (0.99, "PRESENT"),
    ],
)
def test_noul_maps_to_an_outcome_at_the_default_thresholds(present, want):
    assert outcome_from_noul(present, Thresholds()) == want


def test_noul_mapping_follows_custom_thresholds():
    thresholds = Thresholds(possible=0.1, likely=0.2, present=0.3)

    assert outcome_from_noul(0.05, thresholds) == NOT_SURFACED
    assert outcome_from_noul(0.25, thresholds) == "LIKELY"
    assert outcome_from_noul(0.3, thresholds) == "PRESENT"


def test_the_low_noul_outcome_names_both_buckets_it_cannot_tell_apart():
    assert NOT_SURFACED == "ABSENT / UNMENTIONED"


@pytest.mark.parametrize(
    "gate_threshold, want",
    [(0.5, "YES"), (0.9127, "YES"), (0.92, "NO")],
)
def test_gate_noul_is_thresholded_into_yes_or_no(gate_threshold, want):
    [group] = map_answers([CHEST], CHEST_ANSWERS, Thresholds(gate=gate_threshold))

    assert group.gate.noul == 0.9127
    assert group.gate.noul_value == want


def test_gate_choice_reads_option_a_as_the_probability_of_yes():
    [group] = map_answers([CHEST], CHEST_ANSWERS, Thresholds(gate=0.9))

    assert group.gate.choice_yes == 0.85
    assert group.gate.choice_value == "NO"


def test_label_choice_answer_is_reported_with_confidence_and_distribution():
    [group] = map_answers([CHEST], CHEST_ANSWERS, Thresholds())
    pneumonia = group.labels[0]

    assert pneumonia.spec.name == "pneumonia"
    assert pneumonia.choice == "LIKELY"
    assert pneumonia.choice_confidence == 0.5614
    assert pneumonia.choice_probabilities == {
        "PRESENT": 0.2,
        "LIKELY": 0.6491,
        "POSSIBLE": 0.1,
        "ABSENT": 0.03,
        "UNMENTIONED": 0.0209,
    }


def test_label_noul_is_reported_with_its_mapped_outcome():
    [group] = map_answers([CHEST], CHEST_ANSWERS, Thresholds())
    pneumonia, pneumothorax = group.labels

    assert (pneumonia.present, pneumonia.noul_value) == (0.7, "LIKELY")
    assert (pneumothorax.present, pneumothorax.noul_value) == (0.05, NOT_SURFACED)


def test_llm_results_are_shown_and_disagreements_flagged():
    [group] = map_answers(
        [CHEST],
        CHEST_ANSWERS,
        Thresholds(),
        llm_gates={7: "YES"},
        llm_buckets={3: "PRESENT", 4: "ABSENT"},
    )
    pneumonia, pneumothorax = group.labels

    assert group.gate.llm == "YES"
    assert pneumonia.llm == "PRESENT"
    assert pneumonia.choice_differs  # choice says LIKELY
    assert pneumonia.noul_differs  # the noul says LIKELY
    assert pneumothorax.llm == "ABSENT"
    assert not pneumothorax.choice_differs
    assert not pneumothorax.noul_differs


@pytest.mark.parametrize(
    "llm, want_differs",
    [("ABSENT", False), ("UNMENTIONED", False), ("POSSIBLE", True), ("PRESENT", True)],
)
def test_the_low_noul_outcome_agrees_with_either_bucket_the_llm_does_not_surface(llm, want_differs):
    [group] = map_answers([CHEST], CHEST_ANSWERS, Thresholds(), llm_buckets={4: llm})

    assert group.labels[1].noul_value == NOT_SURFACED
    assert group.labels[1].noul_differs is want_differs


@pytest.mark.parametrize("llm", ["ABSENT", "UNMENTIONED"])
def test_a_surfacing_noul_outcome_differs_from_a_bucket_the_llm_does_not_surface(llm):
    [group] = map_answers([CHEST], CHEST_ANSWERS, Thresholds(), llm_buckets={3: llm})

    assert group.labels[0].noul_value == "LIKELY"
    assert group.labels[0].noul_differs


def test_a_gate_answer_that_differs_from_the_llm_is_flagged():
    # The gate answers are 0.85 as choice and 0.9127 as noul, so at 0.9 only the noul says YES.
    [group] = map_answers([CHEST], CHEST_ANSWERS, Thresholds(gate=0.9), llm_gates={7: "YES"})
    assert (group.gate.choice_differs, group.gate.noul_differs) == (True, False)

    [group] = map_answers([CHEST], CHEST_ANSWERS, Thresholds(gate=0.9), llm_gates={7: "NO"})
    assert (group.gate.choice_differs, group.gate.noul_differs) == (False, True)


def test_nothing_is_flagged_as_disagreement_without_an_llm_result():
    [group] = map_answers([CHEST], CHEST_ANSWERS, Thresholds())

    assert group.gate.llm is None
    assert all(label.llm is None for label in group.labels)
    assert not (group.gate.choice_differs or group.gate.noul_differs)
    assert not any(label.choice_differs or label.noul_differs for label in group.labels)


def test_a_no_gate_marks_its_own_column_as_skipped_by_the_pipeline():
    # The gate answers are 0.85 as choice and 0.9127 as noul, so 0.9 separates the two.
    [group] = map_answers([CHEST], CHEST_ANSWERS, Thresholds(gate=0.9), llm_gates={7: "YES"})
    assert (group.llm_skipped, group.choice_skipped, group.noul_skipped) == (False, True, False)

    [group] = map_answers([CHEST], CHEST_ANSWERS, Thresholds(gate=0.5), llm_gates={7: "NO"})
    assert (group.llm_skipped, group.choice_skipped, group.noul_skipped) == (True, False, False)


def test_missing_answers_leave_results_empty_instead_of_failing():
    [group] = map_answers([CHEST], {}, Thresholds(), llm_buckets={3: "PRESENT"})
    pneumonia = group.labels[0]

    assert group.gate.noul is None
    assert group.gate.noul_value is None
    assert group.gate.choice_yes is None
    assert group.gate.choice_value is None
    assert not group.choice_skipped
    assert not group.noul_skipped
    assert pneumonia.choice is None
    assert pneumonia.choice_probabilities == {}
    assert pneumonia.noul_value is None
    assert not pneumonia.choice_differs
    assert not pneumonia.noul_differs


def test_answers_of_the_wrong_shape_leave_results_empty_instead_of_failing():
    answers = {
        "gate_noul:7": "yes",
        "gate_choice:7": {"type": "choice", "choice": "A", "probabilities": ["A", "B"]},
        "bucket:3": {
            "type": "choice",
            "choice": 5,
            "confidence": None,
            "probabilities": {"PRESENT": "high", "LIKELY": 10**400, "ABSENT": 0.25},
        },
        "present:3": {"type": "noul", "noul": True},
        "present:4": ["not", "an", "answer"],
    }

    [group] = map_answers([CHEST], answers, Thresholds())
    pneumonia, pneumothorax = group.labels

    assert (group.gate.noul, group.gate.choice_yes) == (None, None)
    assert pneumonia.choice is None
    assert pneumonia.choice_confidence is None
    assert pneumonia.choice_probabilities == {"ABSENT": 0.25}
    assert (pneumonia.present, pneumonia.noul_value) == (None, None)
    assert (pneumothorax.present, pneumothorax.noul_value) == (None, None)
