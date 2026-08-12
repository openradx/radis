import logging

from django.conf import settings
from procrastinate.contrib.django import app

from radis.core.utils.recovery import sweep_stale_analysis_state

logger = logging.getLogger(__name__)


@app.periodic(cron=settings.ANALYSIS_SWEEP_CRON)
@app.task(queueing_lock="sweep_stale_tasks")
def sweep_stale_tasks_periodic(timestamp: int) -> None:
    # Unlike the startup command this may raise freely: a failed tick just logs, and the
    # queueing_lock prevents pileup.
    sweep_stale_analysis_state()
