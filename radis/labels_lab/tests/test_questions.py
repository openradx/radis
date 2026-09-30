import pytest

from radis.labels.models import LabelResult
from radis.labels_lab.questions import (
    GroupSpec,
    LabelSpec,
    Thresholds,
    bucket_from_nouls,
    build_questions,
    map_answers,
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
    "addressed:3": {"type": "noul", "noul": 0.95},
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
    "addressed:4": {"type": "noul", "noul": 0.9},
    "present:4": {"type": "noul", "noul": 0.05},
}


def test_question_ids_cover_every_group_and_label():
    assert set(build_questions([CHEST])) == {
        "gate_noul:7",
        "gate_choice:7",
        "bucket:3",
        "addressed:3",
        "present:3",
        "bucket:4",
        "addressed:4",
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


def test_label_is_also_asked_as_addressed_and_present_nouls():
    questions = build_questions([CHEST])

    for key in ("addressed:4", "present:4"):
        assert questions[key]["type"] == "noul"
        assert "pneumothorax" in questions[key]["instructions"]
        assert "Air in the pleural space." in questions[key]["instructions"]
    assert questions["addressed:4"]["instructions"] != questions["present:4"]["instructions"]


@pytest.mark.parametrize(
    "addressed, present, want",
    [
        (0.2, 0.9, "UNMENTIONED"),
        (0.49, 0.1, "UNMENTIONED"),
        (0.5, 0.1, "ABSENT"),
        (0.9, 0.34, "ABSENT"),
        (0.9, 0.35, "POSSIBLE"),
        (0.9, 0.59, "POSSIBLE"),
        (0.9, 0.6, "LIKELY"),
        (0.9, 0.84, "LIKELY"),
        (0.9, 0.85, "PRESENT"),
        (0.9, 0.99, "PRESENT"),
    ],
)
def test_two_nouls_map_to_a_bucket_at_the_default_thresholds(addressed, present, want):
    assert bucket_from_nouls(addressed, present, Thresholds()) == want


def test_two_noul_mapping_follows_custom_thresholds():
    thresholds = Thresholds(addressed=0.8, possible=0.1, likely=0.2, present=0.3)

    assert bucket_from_nouls(0.7, 0.9, thresholds) == "UNMENTIONED"
    assert bucket_from_nouls(0.8, 0.25, thresholds) == "LIKELY"
    assert bucket_from_nouls(0.8, 0.3, thresholds) == "PRESENT"


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


def test_label_nouls_are_reported_with_their_mapped_bucket():
    [group] = map_answers([CHEST], CHEST_ANSWERS, Thresholds())
    pneumonia, pneumothorax = group.labels

    assert (pneumonia.addressed, pneumonia.present, pneumonia.nouls_value) == (0.95, 0.7, "LIKELY")
    assert (pneumothorax.addressed, pneumothorax.present, pneumothorax.nouls_value) == (
        0.9,
        0.05,
        "ABSENT",
    )


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
    assert pneumonia.nouls_differs  # nouls say LIKELY
    assert pneumothorax.llm == "ABSENT"
    assert not pneumothorax.choice_differs
    assert not pneumothorax.nouls_differs


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
    assert not any(label.choice_differs or label.nouls_differs for label in group.labels)


def test_a_no_gate_marks_its_own_column_as_skipped_by_the_pipeline():
    # The gate answers are 0.85 as choice and 0.9127 as noul, so 0.9 separates the two.
    [group] = map_answers([CHEST], CHEST_ANSWERS, Thresholds(gate=0.9), llm_gates={7: "YES"})
    assert (group.llm_skipped, group.choice_skipped, group.nouls_skipped) == (False, True, False)

    [group] = map_answers([CHEST], CHEST_ANSWERS, Thresholds(gate=0.5), llm_gates={7: "NO"})
    assert (group.llm_skipped, group.choice_skipped, group.nouls_skipped) == (True, False, False)


def test_missing_answers_leave_results_empty_instead_of_failing():
    [group] = map_answers([CHEST], {}, Thresholds(), llm_buckets={3: "PRESENT"})
    pneumonia = group.labels[0]

    assert group.gate.noul is None
    assert group.gate.noul_value is None
    assert group.gate.choice_yes is None
    assert group.gate.choice_value is None
    assert not group.choice_skipped
    assert not group.nouls_skipped
    assert pneumonia.choice is None
    assert pneumonia.choice_probabilities == {}
    assert pneumonia.nouls_value is None
    assert not pneumonia.choice_differs
    assert not pneumonia.nouls_differs


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
        "addressed:3": {"type": "noul", "noul": True},
        "present:3": ["not", "an", "answer"],
    }

    [group] = map_answers([CHEST], answers, Thresholds())
    pneumonia = group.labels[0]

    assert (group.gate.noul, group.gate.choice_yes) == (None, None)
    assert pneumonia.choice is None
    assert pneumonia.choice_confidence is None
    assert pneumonia.choice_probabilities == {"ABSENT": 0.25}
    assert (pneumonia.addressed, pneumonia.present, pneumonia.nouls_value) == (None, None, None)
