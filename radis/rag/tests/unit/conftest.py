from typing import Callable, Literal, Optional

import pytest

from radis.rag.factories import QuestionFactory, RagInstanceFactory, RagJobFactory, RagTaskFactory
from radis.rag.models import RagJob, RagTask
from radis.reports.factories import LanguageFactory, ReportFactory


@pytest.fixture
def create_rag_task(
    user_with_group,
) -> Callable[[Literal["en", "de"], int, Optional[Literal["Yes", "No"]], int], RagTask]:
    def _create_rag_task(
        language_code: Literal["en", "de"] = "en",
        num_questions: int = 5,
        accepted_answer: Optional[Literal["Yes", "No"]] = None,
        num_rag_instances: int = 5,
    ) -> RagTask:
        language = LanguageFactory.create(code=language_code)

        job = RagJobFactory.create(
            status=RagJob.Status.PENDING,
            owner_id=user_with_group.id,
            owner=user_with_group,
            language=language,
        )

        if accepted_answer is not None:
            QuestionFactory.create_batch(num_questions, job=job, accepted_answer=accepted_answer)
        else:
            QuestionFactory.create_batch(num_questions, job=job)

        task = RagTaskFactory.create(job=job)

        for _ in range(num_rag_instances):
            report = ReportFactory.create(language=language)
            RagInstanceFactory.create(task=task, report=report, other_reports=[])

        return task

    return _create_rag_task
