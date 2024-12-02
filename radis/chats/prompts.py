from string import Template

from django.conf import settings


class Prompt:
    prompt: str

    @classmethod
    def fill(cls, **kwargs) -> str: ...


class QuestionSystemPrompt(Prompt):
    prompt = settings.CHAT_QUESTION_SYSTEM_PROMPT

    @classmethod
    def fill(cls, grammar_instructions: str) -> str:
        return Template(cls.prompt).substitute({"grammar_instructions": grammar_instructions})


class QuestionUserPrompt(Prompt):
    prompt = settings.CHAT_QUESTION_USER_PROMPT

    @classmethod
    def fill(cls, question: str) -> str:
        return Template(cls.prompt).substitute({"question": question})


class ReportQuestionSystemPrompt(Prompt):
    @classmethod
    def fill(cls, grammar_instructions: str, report: str) -> str:
        return Template(cls.prompt).substitute(
            {
                "grammar_instructions": grammar_instructions,
                "report": report,
            }
        )

    prompt = settings.CHAT_REPORT_QUESTION_SYSTEM_PROMPT


class ReportQuestionUserPrompt(Prompt):
    prompt = settings.CHAT_REPORT_QUESTION_USER_PROMPT

    @classmethod
    def fill(cls, question: str) -> str:
        return Template(cls.prompt).substitute({"question": question})
