from django.db.models.signals import post_save
from django.dispatch import receiver

from radis.reports.models import Report

from .models import ReportSearchIndex
from .utils.projection import sync_projection


@receiver(post_save, sender=Report)
def create_or_update_report_search_index(sender, instance, created, **kwargs):
    if created:
        ReportSearchIndex.objects.create(report=instance)
        # Groups and modalities are attached after the Report is created, so
        # they stay empty here and the migration 0004 triggers fill them.
        sync_projection([instance.pk])
        return

    # update_fields is required here, not just an optimization: OneToOneField
    # caches the index on the report instance, so instance.search_index below
    # is often a stale copy from creation time. A bare save() would write all
    # of its fields back, clobbering the projection columns the AFTER UPDATE
    # trigger on reports_report set earlier in this transaction.
    instance.search_index.save(update_fields=["search_vector"])
