import logging

from django.conf import settings
from procrastinate.contrib.django import app

from radis.core.utils.recovery import sweep_stale_analysis_state

logger = logging.getLogger(__name__)


@app.periodic(cron=settings.ANALYSIS_SWEEP_CRON)
@app.task(queueing_lock="sweep_stale_tasks")
def sweep_stale_tasks_periodic(timestamp: int) -> None:
    # A failing run just logs an error; queueing_lock keeps runs from piling up.
    sweep_stale_analysis_state()
