from django.apps import AppConfig
from django.db.models.signals import post_migrate


class ReportsConfig(AppConfig):
    name = "radis.reports"

    def ready(self):
        from .signals import report_signal_processor

        # Put calls to db stuff in this signal handler
        post_migrate.connect(init_db, sender=self)

        report_signal_processor.connect()


def init_db(**kwargs):
    create_app_settings()


def create_app_settings():
    from .models import ReportsAppSettings

    if not ReportsAppSettings.objects.exists():
        ReportsAppSettings.objects.create()
