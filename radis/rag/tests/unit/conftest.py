from typing import Callable, Literal, Optional

import pytest

from radis.core.tests.unit.conftest import openai_chat_completions_mock  # noqa
from radis.rag.factories import QuestionFactory, RagInstanceFactory, RagJobFactory, RagTaskFactory
from radis.rag.models import RagTask
from radis.reports.factories import LanguageFactory, ReportFactory
from radis.reports.models import Language


@pytest.fixture
def create_rag_task(
    user_with_group,
) -> Callable[[Literal["en", "de"], int, Optional[Literal["Y", "N"]], int], RagTask]:
    def _create_rag_task(
        language_code: Literal["en", "de"] = "en",
        num_questions: int = 5,
        accepted_answer: Optional[Literal["Y", "N"]] = None,
        num_rag_instances: int = 5,
    ) -> RagTask:
        job = RagJobFactory.create(
            owner_id=user_with_group.id,
            owner=user_with_group,
            language=LanguageFactory.create(code=language_code),
        )

        if accepted_answer is not None:
            QuestionFactory.create_batch(num_questions, job=job, accepted_answer=accepted_answer)
        else:
            QuestionFactory.create_batch(num_questions, job=job)

        task = RagTaskFactory.create(job=job)
        for _ in range(num_rag_instances):
            report = ReportFactory.create(language=Language.objects.get(code=language_code))
            RagInstanceFactory.create(task=task, reports=[report])

        return task

    return _create_rag_task
