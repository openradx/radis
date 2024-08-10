from django.db.models.signals import post_save
from django.dispatch import receiver

from radis.reports.models import Report

from .models import ReportSearchVector


@receiver(post_save, sender=Report)
def create_or_update_report_search_vector(sender, instance, created, **kwargs):
    if created:
        ReportSearchVector.objects.create(report=instance)
        return

    instance.search_vector.save()
