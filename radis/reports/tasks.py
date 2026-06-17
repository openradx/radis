import logging

from django.core.management import call_command
from procrastinate.contrib.django import app

logger = logging.getLogger(__name__)


@app.periodic(cron="0 * * * *")  # hourly
@app.task
def rebuild_report_overview_stats_task(*args, **kwargs) -> None:
    logger.info("Rebuilding report overview stats")
    call_command("rebuild_report_overview_stats")
