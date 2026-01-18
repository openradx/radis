import logging

from radis.reports.models import Report

logger = logging.getLogger(__name__)


def handle_reports_created(reports: list[Report]) -> None:
    """Handler called when reports are created via API."""
    if not reports:
        return
    # Subscription processing is handled by periodic cron, not event-driven
    logger.info(f"Reports created: {len(reports)} report(s)")


def handle_reports_updated(reports: list[Report]) -> None:
    """Handler called when reports are updated via API."""
    if not reports:
        return
    # Subscription processing is handled by periodic cron, not event-driven
    logger.info(f"Reports updated: {len(reports)} report(s)")
