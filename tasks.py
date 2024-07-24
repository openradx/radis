from pathlib import Path

from adit_radis_shared import invoke_tasks
from adit_radis_shared.invoke_tasks import (  # noqa: F401
    backup_db,
    bump_version,
    format,
    init_workspace,
    lint,
    reset_dev,
    restore_db,
    show_outdated,
    stack_deploy,
    stack_rm,
    test,
    try_github_actions,
    upgrade_adit_radis_shared,
    upgrade_postgresql,
    web_shell,
)
from invoke.context import Context
from invoke.tasks import task

invoke_tasks.PROJECT_NAME = "radis"
invoke_tasks.PROJECT_DIR = Path(__file__).resolve().parent


@task
def compose_up(
    ctx: Context,
    env: invoke_tasks.Environments = "dev",
    no_build: bool = False,
    gpu: bool = False,
):
    """Start containers in specified environment"""
    if gpu:
        profiles = ["gpu"]
    else:
        profiles = ["cpu"]

    invoke_tasks.compose_up(ctx, env=env, no_build=no_build, profile=profiles)


@task
def compose_down(
    ctx: Context,
    env: invoke_tasks.Environments = "dev",
    cleanup: bool = False,
):
    """Stop containers in specified environment"""
    invoke_tasks.compose_down(ctx, env=env, cleanup=cleanup, profile=["cpu", "gpu"])
