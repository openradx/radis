from django.apps import AppConfig
from django.db.models.signals import post_migrate

from radis.reports.site import ReportsCreatedHandler, reports_created_handlers

from .handlers import handle_reports_created


class SubscriptionsConfig(AppConfig):
    name = "radis.subscriptions"

    def ready(self):
        register_app()
        register_reports_handler()

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
    from .models import SubscriptionsAppSettings

    if not SubscriptionsAppSettings.objects.exists():
        SubscriptionsAppSettings.objects.create()


def register_reports_handler():
    """Register handler to trigger subscriptions when reports are created."""

    reports_created_handlers.append(
        ReportsCreatedHandler(
            name="subscription_launcher",
            handle=handle_reports_created,
        )
    )
