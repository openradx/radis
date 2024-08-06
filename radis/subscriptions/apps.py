from django.apps import AppConfig
from django.db.models.signals import post_migrate


class SubscriptionsConfig(AppConfig):
    name = "radis.subscriptions"

    def ready(self):
        register_app()

        # Put calls to db stuff in this signal handler
        post_migrate.connect(init_db, sender=self)


def register_app():
    from adit_radis_shared.common.site import MainMenuItem, register_main_menu_item

    register_main_menu_item(
        MainMenuItem(
            url_name="subscription_list",
            label="Subscriptions",
        )
    )


def init_db(**kwargs):
    create_app_settings()


def create_app_settings():
    from .models import SubscriptionAppSettings

    if not SubscriptionAppSettings.objects.exists():
        SubscriptionAppSettings.objects.create()
