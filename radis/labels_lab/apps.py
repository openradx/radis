from django.apps import AppConfig


class LabelsLabConfig(AppConfig):
    name = "radis.labels_lab"
    verbose_name = "Label Lab"

    def ready(self):
        register_app()


def register_app():
    from adit_radis_shared.common.site import MainMenuItem, register_main_menu_item

    register_main_menu_item(
        MainMenuItem(
            url_name="labels_lab",
            label="Label Lab",
            staff_only=True,
            order=9,
        )
    )
