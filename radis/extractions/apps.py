from django.apps import AppConfig
from django.db.models.signals import post_migrate

SECTION_NAME = "Extractions"


class ExtractionsConfig(AppConfig):
    name = "radis.extractions"

    def ready(self):
        register_app()

        # Put calls to db stuff in this signal handler
        post_migrate.connect(init_db, sender=self)


def register_app():
    from adit_radis_shared.common.site import MainMenuItem, register_main_menu_item

    register_main_menu_item(
        MainMenuItem(
            url_name="extraction_job_create",
            label=SECTION_NAME,
        )
    )


def init_db(**kwargs):
    from .models import ExtractionsAppSettings

    if not ExtractionsAppSettings.objects.exists():
        ExtractionsAppSettings.objects.create()
