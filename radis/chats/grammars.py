import textwrap
from typing import Type

from django.conf import settings

from .models import Grammar


class DefaultGrammar:
    name: str
    human_readable_name: str
    grammar: str
    llm_instruction: str

    is_default = True


predefined_grammars: dict[str, Type[DefaultGrammar]] = {}


def register_grammar(grammar: Type[DefaultGrammar]):
    predefined_grammars[grammar.name] = grammar


class FreeTextGrammar(DefaultGrammar):
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


class YesNoGrammar(DefaultGrammar):
    name = "YES_NO"
    human_readable_name = "Yes or No"
    grammar = settings.CHAT_YES_NO_GRAMMAR
    llm_instruction = "Answer each question in English faithfully with 'Yes' or 'No'."


register_grammar(YesNoGrammar)


class IntegerGrammar(DefaultGrammar):
    name = "INT"
    human_readable_name = "Integer Number"
    grammar = settings.CHAT_INT_GRAMMAR
    llm_instruction = "Answer with an integer number."


register_grammar(IntegerGrammar)


class FloatGrammar(DefaultGrammar):
    name = "FLOAT"
    human_readable_name = "Floating Point Number"
    grammar = settings.CHAT_FLOAT_GRAMMAR
    llm_instruction = "Answer with a floating point number."


register_grammar(FloatGrammar)


class DateGrammar(DefaultGrammar):
    name = "DATE"
    human_readable_name = "Date"
    grammar = settings.CHAT_DATE_GRAMMAR
    llm_instruction = "Answer with a date in format 'DD/MM/YYYY'."


register_grammar(DateGrammar)


class TimeGrammar(DefaultGrammar):
    name = "TIME"
    human_readable_name = "Time"
    grammar = settings.CHAT_TIME_GRAMMAR
    llm_instruction = "Answer with a time in format 'HH:MM'."


register_grammar(TimeGrammar)


class DateTimeGrammar(DefaultGrammar):
    name = "DATETIME"
    human_readable_name = "Date and Time"
    grammar = settings.CHAT_DATETIME_GRAMMAR
    llm_instruction = "Answer with a date and time in format 'DD/MM/YYYY HH:MM'."


register_grammar(DateTimeGrammar)


def create_default_grammars():
    for grammar in predefined_grammars.values():
        Grammar.objects.get_or_create(
            name=grammar.name,
            human_readable_name=grammar.human_readable_name,
            grammar=grammar.grammar,
            llm_instruction=grammar.llm_instruction,
            is_default=grammar.is_default,
        )
