from datetime import datetime
from typing import Any, Iterable

from celery import shared_task
from celery.utils.log import get_task_logger
from vespa.io import VespaResponse

from radis.reports.models import Report
from radis.vespa.utils.document_utils import dictify_report_for_vespa

from .models import VespaReFeed
from .vespa_app import vespa_app

logger = get_task_logger(__name__)


@shared_task
def re_feed_reports(id: int):
    """
    A celery shared task to re-feed reports from the PostgreSQL database to Vespa.
    It takes an id of the ReFeed modal as input and logs the progress and errors, if any.
    Such a re-feed is necessary when schema changes in a specific way.
    For more information see:
    - https://docs.vespa.ai/en/schemas.html#schema-modifications
    - https://docs.vespa.ai/en/reference/schema-reference.html#modifying-schemas
    """

    def log(msg: str, save: bool = True):
        logger.info(msg)
        re_feed.log += f"{msg}\n"
        if save:
            re_feed.save()

    try:
        re_feed = VespaReFeed.objects.get(id)
        assert re_feed.status == VespaReFeed.PENDING

        log(f"Re-feed {re_feed.id} started", save=False)
        re_feed.status = VespaReFeed.IN_PROGRESS
        re_feed.started = datetime.now()
        log("Deleting all report documents from Vespa", save=False)
        re_feed.save()

        response = vespa_app.get_client().delete_all_docs("radis_content", "reports")

        if response.status_code != 200:
            raise Exception(f"Error while deleting all documents from Vespa: {response.text}")

        log("Re-feeding report documents")

        def reports_generator(reports_to_feed: Iterable[Report]) -> Iterable[dict[str, Any]]:
            for report in reports_to_feed:
                yield dictify_report_for_vespa(report)

        def feed_callback(response: VespaResponse, doc_id: str):
            if response.get_status_code() != 200:
                raise Exception(
                    f"Error while re-feeding report document {doc_id}: {response.get_json()}"
                )
            re_feed.progress_count += 1
            re_feed.save()

        reports = Report.objects.all()
        vespa_app.get_client().feed_iterable(
            reports_generator(reports), "reports", callback=feed_callback
        )

        log("Re-feeding finished", save=False)
        re_feed.status = VespaReFeed.SUCCESS
        re_feed.ended = datetime.now()
        re_feed.save()
    except Exception as ex:
        log(f"Re-feeding failed - {ex}")
        re_feed.status = VespaReFeed.FAILURE
        re_feed.save()
