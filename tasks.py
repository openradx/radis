import json
import time
from pathlib import Path
from random import randint
from typing import Literal

import openai
from adit_radis_shared import invoke_tasks
from adit_radis_shared.invoke_tasks import (  # noqa: F401
    backup_db,
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
from openai import OpenAI
from openai.types.chat import ChatCompletion, ChatCompletionMessageParam

invoke_tasks.PROJECT_NAME = "radis"
invoke_tasks.PROJECT_DIR = Path(__file__).resolve().parent

SYSTEM_PROMPT = {
    "de": "Du bist ein Radiologe.",
    "en": "You are a radiologist.",
}

USER_PROMPT = {
    "de": "Schreibe einen radiologischen Befund.",
    "en": "Write a radiology report.",
}


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
    invoke_tasks.compose_down(ctx, env=env, cleanup=cleanup, profile=["cpu", "gpu", "mock"])


@task
def generate_example_reports(
    ctx: Context,
    out: str,
    count: int = 10,
    model: str = "gpt-3.5-turbo",
    lng: Literal["en", "de"] = "en",
    overwrite: bool = False,
):
    """Generate example reports"""
    print(f"Generating {count} example reports...")

    config = Utility.load_config_from_env_file()

    openai_api_key = config.get("OPENAI_API_KEY")
    if not openai_api_key:
        raise Exit("Missing OPENAI_API_KEY setting in .env file")

    out_path = Path(out)
    if out_path.exists() and not overwrite:
        raise Exit(f"File '{out_path.absolute()}' already exists.")

    client = OpenAI(api_key=openai_api_key)
    messages: list[ChatCompletionMessageParam] = [
        {"role": "system", "content": SYSTEM_PROMPT[lng]},
        {"role": "user", "content": USER_PROMPT[lng]},
    ]

    start = time.time()
    report_bodies: list[str] = []
    for _ in range(count):
        response: ChatCompletion | None = None
        retries = 0
        while not response:
            try:
                response = client.chat.completions.create(
                    messages=messages,
                    model=model,
                )
            # For available errors see https://github.com/openai/openai-python#handling-errors
            except openai.APIStatusError as err:
                retries += 1
                if retries == 3:
                    print(f"Error! Service unavailable even after 3 retries: {err}")
                    raise err

                # maybe use rate limiter like https://github.com/tomasbasham/ratelimit
                time.sleep(randint(1, 5))

        content = response.choices[0].message.content
        assert content
        report_bodies.append(content)
        print(".", end="", flush=True)
    print("")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(report_bodies, f, indent=4)

    print(f"Done in {time.time() - start:.2f}s")
    print(f"Example reports written to '{out_path.absolute()}'")
