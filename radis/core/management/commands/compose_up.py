from typing import Annotated

from adit_radis_shared.maintenance_commands.management.commands.compose_up import (
    Command as ComposeUpCommand,
)
from typer import Option


class Command(ComposeUpCommand):
    """Start stack with docker compose"""

    def handle(
        self,
        no_build: Annotated[bool, Option(help="Do not build images")] = False,
        profile: Annotated[list[str], Option(help="Docker Compose profile(s)")] = [],
        simulate: Annotated[bool, Option(help="Simulate the command")] = False,
    ):
        """Start development containers"""
        config = self.load_config_from_env_file()
        if str(config.get("GPU_INFERENCE_ENABLED", "")).lower() in ["yes", "true", "1"]:
            profiles = ["gpu"]
        else:
            profiles = ["cpu"]

        super().handle(no_build=no_build, profile=profiles, simulate=simulate)
