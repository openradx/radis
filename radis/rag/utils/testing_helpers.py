from typing import Literal

from adit_radis_shared.accounts.factories import GroupFactory, UserFactory
from adit_radis_shared.common.utils.testing_helpers import add_user_to_group

from radis.rag.factories import QuestionFactory, RagInstanceFactory, RagJobFactory, RagTaskFactory
from radis.rag.models import RagJob, RagTask
from radis.reports.factories import LanguageFactory, ReportFactory


def create_rag_task(
    language_code: Literal["en", "de"] = "en",
    num_questions: int = 5,
    accepted_answer: Literal["Y", "N"] | None = None,
    num_rag_instances: int = 5,
) -> RagTask:
    language = LanguageFactory.create(code=language_code)

    user = UserFactory()
    group = GroupFactory()
    add_user_to_group(user, group)
    job = RagJobFactory.create(
        status=RagJob.Status.PENDING,
        owner_id=user.id,
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
