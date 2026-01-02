import logging
import time

from radis.reports.models import Report
from radis.subscriptions.tasks import subscription_launcher

logger = logging.getLogger(__name__)


def handle_reports_created(reports: list[Report]) -> None:
    """
    Handler called when reports are created via API.
    Triggers subscription processing for bulk imports.
    """
    if not reports:
        return

    # Trigger subscriptions for any report creation
    # The subscription_launcher will filter by last_refreshed timestamp
    logger.info(f"Triggering subscription processing for {len(reports)} new report(s)")
    subscription_launcher.defer(timestamp=int(time.time()))
