import pytest
from pydantic import ValidationError

from radis.labels.factories import QuestionFactory
from radis.labels.prompts import build_yes_no_maybe_schema, sanitize_label


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("pneumonia", "pneumonia"),
        ("Lung Effusion", "lung_effusion"),
        ("foo-bar", "foo_bar"),
        ("123abc", "_123abc"),
        ("a b c", "a_b_c"),
    ],
)
def test_sanitize_label(raw, expected):
    out = sanitize_label(raw)
    assert out == expected
    assert out.isidentifier()


class TestBuildSchema:
    def test_one_field_per_question(self):
        q1 = QuestionFactory(label="pneumonia")
        q2 = QuestionFactory(label="effusion")
        Schema = build_yes_no_maybe_schema([q1, q2])
        inst = Schema(pneumonia="YES", effusion="MAYBE")
        dumped = inst.model_dump()
        assert dumped["pneumonia"] == "YES" and dumped["effusion"] == "MAYBE"

    def test_rejects_unknown_value(self):
        Schema = build_yes_no_maybe_schema([QuestionFactory(label="x")])
        with pytest.raises(ValidationError):
            Schema(x="PROBABLY")

    def test_rejects_extra_field(self):
        Schema = build_yes_no_maybe_schema([QuestionFactory(label="x")])
        with pytest.raises(ValidationError):
            Schema(x="YES", extra="YES")

    def test_rejects_missing_field(self):
        Schema = build_yes_no_maybe_schema([QuestionFactory(label="x"), QuestionFactory(label="y")])
        with pytest.raises(ValidationError):
            Schema(x="YES")

    def test_label_collision_raises(self):
        q1 = QuestionFactory(label="lung effusion")
        q2 = QuestionFactory(label="lung-effusion")
        with pytest.raises(ValueError, match="collide"):
            build_yes_no_maybe_schema([q1, q2])
