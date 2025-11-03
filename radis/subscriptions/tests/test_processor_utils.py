from dataclasses import dataclass

from radis.extractions.models import OutputType
from radis.subscriptions.utils.processor_utils import (
    build_extraction_schema,
    build_filter_schema,
    generate_extraction_fields_prompt,
    generate_filter_questions_prompt,
)


@dataclass
class DummyFilterQuestion:
    pk: int
    question: str
    expected_answer_bool: bool


class DummyFilterQuestionSet:
    def __init__(self, *questions: DummyFilterQuestion):
        self._questions = list(questions)

    def all(self):
        return list(self._questions)


@dataclass
class DummyExtractionField:
    pk: int
    name: str
    description: str
    output_type: str

    def get_output_type_display(self) -> str:
        return {
            OutputType.TEXT: "Text",
            OutputType.NUMERIC: "Numeric",
            OutputType.BOOLEAN: "Boolean",
        }[self.output_type]


class DummyExtractionFieldSet:
    def __init__(self, *fields: DummyExtractionField):
        self._fields = list(fields)

    def all(self):
        return list(self._fields)


def test_build_subscription_schema_and_prompts_without_database():
    questions = DummyFilterQuestionSet(
        DummyFilterQuestion(pk=1, question="Contains pneumonia?", expected_answer_bool=True)
    )
    fields = DummyExtractionFieldSet(
        DummyExtractionField(
            pk=10,
            name="diagnosis",
            description="Primary diagnosis mentioned in the report",
            output_type=OutputType.TEXT,
        )
    )

    filter_bundle = build_filter_schema(questions)
    extraction_bundle = build_extraction_schema(fields)

    assert len(filter_bundle.mapping) == 1
    assert len(extraction_bundle.mapping) == 1

    filter_field_name, mapped_question = filter_bundle.mapping[0]
    assert filter_field_name.startswith("filter_")
    assert mapped_question.pk == 1

    extraction_field_name, mapped_field = extraction_bundle.mapping[0]
    assert extraction_field_name.startswith("extraction_")
    assert mapped_field.pk == 10

    filter_prompt = generate_filter_questions_prompt(filter_bundle.mapping)
    assert "filter_" in filter_prompt
    assert "Contains pneumonia?" in filter_prompt

    extraction_prompt = generate_extraction_fields_prompt(extraction_bundle.mapping)
    assert "extraction_" in extraction_prompt
    assert "diagnosis" in extraction_prompt
