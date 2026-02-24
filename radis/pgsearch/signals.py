from django.db import transaction
from django.db.models.signals import post_save
from django.dispatch import receiver

from radis.reports.models import Report

from .models import ReportSearchVector
from .tasks import generate_report_embedding
from .utils.embedding_client import is_embedding_available


@receiver(post_save, sender=Report)
def create_or_update_report_search_vector(sender, instance, created, **kwargs):
    if created:
        ReportSearchVector.objects.create(report=instance)
    else:
        instance.search_vector.save()

    if is_embedding_available():
        transaction.on_commit(lambda: generate_report_embedding.defer(report_id=instance.id))
