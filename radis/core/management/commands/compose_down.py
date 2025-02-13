from typing import Annotated

from adit_radis_shared.maintenance_commands.management.commands.compose_down import (
    Command as ComposeDownCommand,
)
from typer import Option


class Command(ComposeDownCommand):
    """Stop stack with docker compose"""

    def handle(
        self,
        cleanup: Annotated[bool, Option(help="Remove orphans and volumes")] = False,
        profile: Annotated[list[str], Option(help="Docker Compose profile(s)")] = [],
        simulate: Annotated[bool, Option(help="Simulate the command")] = False,
    ):
        super().handle(cleanup=cleanup, profile=profile, simulate=simulate)
