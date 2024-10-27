import textwrap
from datetime import datetime
from typing import Literal, Type

from django.conf import settings


class Grammar:
    name: str
    human_readable_name: str
    grammar: str
    llm_instruction: str

    @staticmethod
    def validate(string: str) -> str: ...

    @staticmethod
    def to_string(value) -> str:
        return str(value)

    @staticmethod
    def from_string(string: str):
        return string


predefined_grammars: dict[str, Type[Grammar]] = {}


def register_grammar(grammar: Type[Grammar]):
    predefined_grammars[grammar.name] = grammar


class FreeTextGrammar(Grammar):
    name = "FREE_TEXT"
    human_readable_name = "Free Text"
    grammar = settings.CHAT_FREE_TEXT_GRAMMAR
    llm_instruction = textwrap.dedent(
        """Provide concise, well-structured answers in the same language used in the question. Do
        use appropriate medical terminology. Use headers to organize information when necessary.
        Include relevant anatomical details, imaging modalities, and diagnostic considerations
        where applicable. Base your responses on current, peer-reviewed medical literature and
        established radiological guidelines. If there are conflicting views or ongoing debates in
        the field, acknowledge them briefly."""
    )

    @staticmethod
    def validate(string: str) -> str:
        return string


class YesNoGrammar(Grammar):
    name = "YES_NO"
    human_readable_name = "Yes or No"
    grammar = settings.CHAT_YES_NO_GRAMMAR
    llm_instruction = "Answer each question in English faithfully with 'Yes' or 'No'."

    @staticmethod
    def validate(string: str) -> Literal["yes", "no"]:
        if string not in ["Yes", "No"]:
            raise ValueError(f"Expected Yes or No, got {string}")
        return "yes" if string == "Yes" else "no"


register_grammar(YesNoGrammar)


class IntegerGrammar(Grammar):
    name = "INT"
    human_readable_name = "Integer Number"
    grammar = settings.CHAT_INT_GRAMMAR
    llm_instruction = "Answer with an integer number."

    @staticmethod
    def validate(string: str) -> str:
        try:
            int(string)
        except ValueError:
            raise ValueError(f"Expected integer, got {string}")
        return string

    @staticmethod
    def from_string(string: str) -> int:
        return int(string)


register_grammar(IntegerGrammar)


class FloatGrammar(Grammar):
    name = "FLOAT"
    human_readable_name = "Floating Point Number"
    grammar = settings.CHAT_FLOAT_GRAMMAR
    llm_instruction = "Answer with a floating point number."

    @staticmethod
    def validate(string: str) -> str:
        try:
            float(string)
        except ValueError:
            raise ValueError(f"Expected float, got {string}")
        return string

    @staticmethod
    def from_string(string: str) -> float:
        return float(string)


register_grammar(FloatGrammar)


class DateGrammar(Grammar):
    name = "DATE"
    human_readable_name = "Date"
    grammar = settings.CHAT_DATE_GRAMMAR
    llm_instruction = "Answer with a date in format 'DD/MM/YYYY'."

    @staticmethod
    def validate(string: str) -> str:
        try:
            datetime.strptime(string, "%d/%m/%Y")
        except ValueError:
            raise ValueError(f"Expected date in format DD/MM/YYYY, got {string}")
        return string

    @staticmethod
    def to_string(value) -> str:
        return value.strftime("%d/%m/%Y")

    @staticmethod
    def from_string(string: str) -> datetime:
        return datetime.strptime(string, "%d/%m/%Y")


register_grammar(DateGrammar)


class TimeGrammar(Grammar):
    name = "TIME"
    human_readable_name = "Time"
    grammar = settings.CHAT_TIME_GRAMMAR
    llm_instruction = "Answer with a time in format 'HH:MM'."

    @staticmethod
    def validate(string: str) -> str:
        try:
            datetime.strptime(string, "%H:%M")
        except ValueError:
            raise ValueError(f"Expected time in format HH:MM, got {string}")
        return string

    @staticmethod
    def to_string(value) -> str:
        return value.strftime("%H:%M")

    @staticmethod
    def from_string(string: str) -> datetime:
        return datetime.strptime(string, "%H:%M")


register_grammar(TimeGrammar)


class DateTimeGrammar(Grammar):
    name = "DATETIME"
    human_readable_name = "Date and Time"
    grammar = settings.CHAT_DATETIME_GRAMMAR
    llm_instruction = "Answer with a date and time in format 'DD/MM/YYYY HH:MM'."

    @staticmethod
    def validate(string: str) -> str:
        try:
            datetime.strptime(string, "%d/%m/%Y %H:%M")
        except ValueError:
            raise ValueError(f"Expected date and time in format DD/MM/YYYY HH:MM, got {string}")
        return string

    @staticmethod
    def to_string(value) -> str:
        return value.strftime("%d/%m/%Y %H:%M")

    @staticmethod
    def from_string(string: str) -> datetime:
        return datetime.strptime(string, "%d/%m/%Y %H:%M")


register_grammar(DateTimeGrammar)
