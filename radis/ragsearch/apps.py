from django.apps import AppConfig
from django.db.models.signals import post_migrate

from radis.core.site import register_main_menu_item


def register_app():
    register_main_menu_item(
        url_name="ragsearch",
        label="RAG-Search",
    )


def init_db(**kwargs):
    create_app_settings()


def create_app_settings():
    from .models import RagsearchAppSettings

    settings = RagsearchAppSettings.get()
    if not settings:
        RagsearchAppSettings.objects.create()


class RagsearchConfig(AppConfig):
    name = "radis.ragsearch"

    def ready(self):
        register_app()

        # Put calls to db stuff in this signal handler
        post_migrate.connect(init_db, sender=self)
