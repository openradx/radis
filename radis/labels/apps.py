from django.apps import AppConfig


class LabelsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "radis.labels"

    def ready(self) -> None:
        from .signals import register_report_handlers

        register_report_handlers()
