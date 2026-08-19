from django.conf import settings
from procrastinate.contrib.django import app

from radis.core.utils.recovery import sweep_stale_analysis_state


@app.periodic(cron=settings.ANALYSIS_SWEEP_CRON)
@app.task(queueing_lock="sweep_stale_tasks")
def sweep_stale_tasks_periodic(timestamp: int) -> None:
    # If a run fails, Procrastinate logs the traceback and the next tick tries again;
    # queueing_lock keeps ticks from piling up while one is still queued.
    sweep_stale_analysis_state()
