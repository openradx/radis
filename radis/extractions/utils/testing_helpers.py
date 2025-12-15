from typing import Literal

from adit_radis_shared.accounts.factories import GroupFactory, UserFactory
from adit_radis_shared.common.utils.testing_helpers import add_user_to_group

from radis.extractions.factories import (
    ExtractionInstanceFactory,
    ExtractionJobFactory,
    ExtractionTaskFactory,
    OutputFieldFactory,
)
from radis.extractions.models import ExtractionJob, ExtractionTask
from radis.reports.factories import LanguageFactory, ReportFactory


def create_extraction_task(
    language_code: Literal["en", "de"] = "en",
    num_output_fields: int = 5,
    num_extraction_instances: int = 5,
) -> ExtractionTask:
    language = LanguageFactory.create(code=language_code)

    user = UserFactory()
    group = GroupFactory()
    add_user_to_group(user, group)
    job = ExtractionJobFactory.create(
        status=ExtractionJob.Status.PENDING,
        owner=user,
        group=group,
        language=language,
    )

    OutputFieldFactory.create_batch(num_output_fields, job=job)

    task = ExtractionTaskFactory.create(job=job)

    for _ in range(num_extraction_instances):
        report = ReportFactory.create(language=language)
        ExtractionInstanceFactory.create(task=task, report=report)

    return task
