from django.apps import AppConfig


class ReportGeneratorConfig(AppConfig):
    name = "radis.report_generator"
    verbose_name = "Report Generator"

    def ready(self) -> None:  # pragma: no cover - registration side effect
        from adit_radis_shared.common.site import MainMenuItem, register_main_menu_item

        register_main_menu_item(
            MainMenuItem(
                url_name="report_generator:generate",
                label="Report Generator",
                order=95,
                staff_only=True,
            )
        )
