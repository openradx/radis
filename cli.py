#! /usr/bin/env python3

import json
import socket
import sys
import time
from pathlib import Path
from random import randint
from typing import Annotated

import openai
import typer
from adit_radis_shared import cli_commands as commands
from adit_radis_shared import cli_helpers as helpers
from openai.types.chat import ChatCompletion, ChatCompletionMessageParam

helpers.PROJECT_ID = "radis"
helpers.ROOT_PATH = Path(__file__).resolve().parent

app = typer.Typer()

extra_args = {"allow_extra_args": True, "ignore_unknown_options": True}

app.command()(commands.init_workspace)
app.command()(commands.stack_deploy)
app.command()(commands.stack_rm)
app.command()(commands.lint)
app.command()(commands.format_code)
app.command(context_settings=extra_args)(commands.test)
app.command()(commands.show_outdated)
app.command()(commands.backup_db)
app.command()(commands.restore_db)
app.command()(commands.shell)
app.command()(commands.generate_certificate_files)
app.command()(commands.generate_certificate_chain)
app.command()(commands.generate_django_secret_key)
app.command()(commands.generate_secure_password)
app.command()(commands.generate_auth_token)
app.command()(commands.randomize_env_secrets)
app.command()(commands.try_github_actions)


@app.command()
def compose_up(
    build: Annotated[bool, typer.Option(help="Do not build images")] = True,
    profile: Annotated[list[str], typer.Option(help="Docker Compose profile(s)")] = [],
):
    """Start stack with docker compose"""

    config = helpers.load_config_from_env_file()
    use_external_llm = bool(config.get("EXTERNAL_LLM_PROVIDER_URL", ""))
    use_gpu = str(config.get("LLAMACPP_USE_GPU", "")).lower() in ["yes", "true", "1"]

    if use_external_llm:
        profiles = profile
    else:
        if use_gpu:
            profiles = profile + ["llamacpp_gpu"]
        else:
            profiles = profile + ["llamacpp_cpu"]

    commands.compose_up(build=build, profile=profiles)


@app.command()
def compose_down(
    cleanup: Annotated[bool, typer.Option(help="Remove orphans and volumes")] = False,
    profile: Annotated[list[str], typer.Option(help="Docker Compose profile(s)")] = [],
):
    """Stop stack with docker compose"""

    config = helpers.load_config_from_env_file()
    if str(config.get("GPU_INFERENCE_ENABLED", "")).lower() in ["yes", "true", "1"]:
        profiles = profile + ["gpu"] if "gpu" not in profile else profile
    else:
        profiles = profile + ["cpu"] if "cpu" not in profile else profile

    commands.compose_down(cleanup=cleanup, profile=profiles)


SYSTEM_PROMPT = {
    "de": "Du bist ein Radiologe.",
    "en": "You are a radiologist.",
}

USER_PROMPT = {
    "de": "Schreibe einen radiologischen Befund.",
    "en": "Write a radiology report.",
}


@app.command()
def generate_example_reports(
    out: Annotated[str, typer.Option(help="Output file")] = "example_reports.json",
    count: Annotated[int, typer.Option(help="Number of reports to generate")] = 10,
    model: Annotated[str, typer.Option(help="OpenAI model")] = "gpt-3.5-turbo",
    lng: Annotated[str, typer.Option(help="Language")] = "en",
    overwrite: Annotated[bool, typer.Option(help="Overwrite existing file")] = False,
):
    """Generate example reports"""

    print(f"Generating {count} example reports...")

    config = helpers.load_config_from_env_file()

    openai_api_key = config.get("OPENAI_API_KEY")
    if not openai_api_key:
        sys.exit("Missing OPENAI_API_KEY setting in .env file")

    out_path = Path(out)
    if out_path.exists() and not overwrite:
        sys.exit(f"File '{out_path.absolute()}' already exists.")

    client = openai.OpenAI(api_key=openai_api_key)
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


@app.command()
def host_ip():
    """Get the IP of the Docker host"""

    hostname = "host.docker.internal"
    try:
        ip_address = socket.gethostbyname(hostname)
        print(f"The IP address of the Docker host is: {ip_address}")
    except Exception as e:
        print(f"Error resolving {hostname}: {e}")


if __name__ == "__main__":
    app()
