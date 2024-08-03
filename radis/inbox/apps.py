from django.apps import AppConfig
from django.db.models.signals import post_migrate


class InboxConfig(AppConfig):
    name = "radis.inbox"

    def ready(self):
        register_app()

        # Put calls to db stuff in this signal handler
        post_migrate.connect(init_db, sender=self)


def register_app():
    from adit_radis_shared.common.site import MainMenuItem, register_main_menu_item

    register_main_menu_item(
        MainMenuItem(
            url_name="inbox_list",
            label="Inbox",
        )
    )


def init_db(**kwargs):
    create_app_settings()


def create_app_settings():
    from .models import InboxAppSettings

    if not InboxAppSettings.objects.exists():
        InboxAppSettings.objects.create()
        InboxAppSettings.objects.create()
