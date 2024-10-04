from django.apps import AppConfig
from django.db.models.signals import post_migrate


class ReportsConfig(AppConfig):
    name = "radis.reports"

    def ready(self):
        # Put calls to db stuff in this signal handler
        post_migrate.connect(init_db, sender=self)


def init_db(**kwargs):
    from .models import ReportsAppSettings

    if not ReportsAppSettings.objects.exists():
        ReportsAppSettings.objects.create()
