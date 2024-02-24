from django.apps import AppConfig

SECTION_NAME = "Sandbox"


class SandboxConfig(AppConfig):
    name = "radis.sandbox"

    def ready(self):
        register_app()


def register_app():
    from radis.core.site import register_main_menu_item

    register_main_menu_item(
        url_name="sandbox_list",
        label=SECTION_NAME,
    )
