from django.apps import AppConfig
from django.db.models.signals import post_migrate


class CollectionsConfig(AppConfig):
    name = "radis.collections"

    def ready(self):
        register_app()

        # Put calls to db stuff in this signal handler
        post_migrate.connect(init_db, sender=self)


def register_app():
    from adit_radis_shared.common.site import MainMenuItem, register_main_menu_item

    from radis.reports.site import register_report_panel_button

    register_main_menu_item(
        MainMenuItem(
            url_name="collection_list",
            label="Collections",
        )
    )

    register_report_panel_button(1, "collections/_collection_select_button.html")


def init_db(**kwargs):
    create_app_settings()


def create_app_settings():
    from .models import CollectionsAppSettings

    if not CollectionsAppSettings.objects.exists():
        CollectionsAppSettings.objects.create()
