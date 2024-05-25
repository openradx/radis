import logging

from django.db import transaction
from django.db.models.signals import post_delete, post_save

from radis.reports.models import Report

from .site import reports_created_handlers, reports_deleted_handlers, reports_updated_handlers

logger = logging.getLogger(__name__)


class ReportSignalProcessor:
    """Watch for changes of a report and trigger a re-index.

    Cave! This only works well for the Django admin interface of the reports model as this
    always trigger a post_save signal when a report is saved regardless of what field is
    updated (even m2m fields).
    """

    paused = False

    def connect(self):
        post_save.connect(self._handle_save, sender=Report)
        post_delete.connect(self._handle_delete, sender=Report)

    def disconnect(self):
        post_save.disconnect(self._handle_save, sender=Report)
        post_delete.disconnect(self._handle_delete, sender=Report)

    def pause(self):
        self.paused = True

    def resume(self):
        self.paused = False

    def _handle_save(self, sender: type[Report], instance: Report, created: bool, **kwargs):
        if self.paused:
            return

        if created:
            logger.debug("Received a signal that a report has been created: %s", instance)
            transaction.on_commit(
                lambda: [handler.handle([instance.id]) for handler in reports_created_handlers]
            )
        else:
            logger.debug("Received a signal that a report has been updated: %s", instance)
            transaction.on_commit(
                lambda: [handler.handle([instance.id]) for handler in reports_updated_handlers]
            )

    def _handle_delete(self, sender: type[Report], instance: Report, **kwargs):
        if self.paused:
            return

        logger.debug("Received a signal that a report has been deleted: %s", instance)
        transaction.on_commit(
            lambda: [handler.handle([instance.document_id]) for handler in reports_deleted_handlers]
        )


report_signal_processor = ReportSignalProcessor()
