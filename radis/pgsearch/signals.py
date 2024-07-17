from django.db.models.signals import pre_save
from django.dispatch import receiver

from ..reports.models import Report


@receiver(pre_save, sender=Report)
def update_report_search_vector(sender, instance, **kwargs):
    instance.update_search_vector()
