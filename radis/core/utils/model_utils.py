from typing import TYPE_CHECKING

from django.db import models

if TYPE_CHECKING:
    from ..models import AnalysisTask


def reset_tasks(tasks: models.QuerySet["AnalysisTask"]) -> None:
    tasks.update(
        status=tasks.model.Status.PENDING,
        queued_job_id=None,
        attempts=0,
        message="",
        log="",
        started_at=None,
        ended_at=None,
    )
