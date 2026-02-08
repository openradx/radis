from django.apps import AppConfig


class LabelsConfig(AppConfig):
    name = "radis.labels"

    def ready(self) -> None:
        register_app()

        from radis.reports.site import (
            ReportsCreatedHandler,
            ReportsUpdatedHandler,
            register_reports_created_handler,
            register_reports_updated_handler,
        )

        from . import signals
        from .site import handle_reports_created, handle_reports_updated

        register_reports_created_handler(
            ReportsCreatedHandler(
                name="Labels",
                handle=handle_reports_created,
            )
        )
        register_reports_updated_handler(
            ReportsUpdatedHandler(
                name="Labels",
                handle=handle_reports_updated,
            )
        )


def register_app() -> None:
    from adit_radis_shared.common.site import MainMenuItem, register_main_menu_item

    register_main_menu_item(
        MainMenuItem(
            url_name="label_group_list",
            label="Labels",
        )
    )
