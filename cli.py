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
from adit_radis_shared.cli import commands
from adit_radis_shared.cli import helper as cli_helper
from openai.types.chat import ChatCompletion, ChatCompletionMessageParam

app = typer.Typer(
    context_settings={"help_option_names": ["-h", "--help"]},
    no_args_is_help=True,
)

app.command()(commands.init_workspace)
app.command()(commands.randomize_env_secrets)
app.command()(commands.compose_pull)
app.command()(commands.stack_deploy)
app.command()(commands.stack_rm)
app.command()(commands.lint)
app.command()(commands.format_code)
app.command()(commands.test)
app.command()(commands.shell)
app.command()(commands.show_outdated)
app.command()(commands.db_backup)
app.command()(commands.db_restore)
app.command()(commands.generate_auth_token)
app.command()(commands.generate_secure_password)
app.command()(commands.generate_django_secret_key)
app.command()(commands.generate_certificate_chain)
app.command()(commands.generate_certificate_files)
app.command()(commands.upgrade_postgres_volume)
app.command()(commands.try_github_actions)


@app.command()
def compose_build(
    profile: Annotated[
        list[str] | None, typer.Option(help="Docker compose profile(s) to use")
    ] = None,
    extra_args: Annotated[
        list[str] | None, typer.Argument(help="Extra arguments (after '--')")
    ] = None,
):
    """Build the base images with docker compose"""

    profile = profile or []
    extra_args = extra_args or []

    helper = cli_helper.CommandHelper()

    config = helper.load_config_from_env_file()
    use_external_llm = bool(config.get("EXTERNAL_LLM_PROVIDER_URL", ""))
    use_gpu = str(config.get("LLAMACPP_USE_GPU", "")).lower() in ["yes", "true", "1"]

    if use_external_llm:
        profiles = profile
    else:
        if use_gpu:
            profiles = profile + ["llamacpp_gpu"]
        else:
            profiles = profile + ["llamacpp_cpu"]

    commands.compose_build(profile=profiles, extra_args=extra_args)


@app.command()
def compose_up(
    profile: Annotated[
        list[str] | None, typer.Option(help="Docker compose profile(s) to use")
    ] = None,
    extra_args: Annotated[
        list[str] | None, typer.Argument(help="Extra arguments (after '--')")
    ] = None,
):
    """Start stack with docker compose"""

    profile = profile or []
    extra_args = extra_args or []

    helper = cli_helper.CommandHelper()

    config = helper.load_config_from_env_file()
    use_external_llm = bool(config.get("EXTERNAL_LLM_PROVIDER_URL", ""))
    use_gpu = str(config.get("LLAMACPP_USE_GPU", "")).lower() in ["yes", "true", "1"]

    if use_external_llm:
        profiles = profile
    else:
        if use_gpu:
            profiles = profile + ["gpu"]
        else:
            profiles = profile + ["cpu"]

    print(f"Using profiles: {profiles}")

    commands.compose_up(profile=profiles, extra_args=extra_args)


@app.command()
def compose_down(
    profile: Annotated[
        list[str] | None, typer.Option(help="Docker compose profile(s) to use")
    ] = None,
    extra_args: Annotated[
        list[str] | None, typer.Argument(help="Extra arguments (after '--')")
    ] = None,
):
    """Stop stack with docker compose"""

    profile = profile or []
    extra_args = extra_args or []

    profiles = [*profile, "gpu", "cpu"]

    commands.compose_down(profile=profiles, extra_args=extra_args)


@app.command()
def get_host_ip():
    """Get the IP of the Docker host"""

    hostname = "host.docker.internal"
    try:
        ip_address = socket.gethostbyname(hostname)
        print(f"The IP address of the Docker host is: {ip_address}")
    except Exception as e:
        print(f"Error resolving {hostname}: {e}")


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

    helper = cli_helper.CommandHelper()
    config = helper.load_config_from_env_file()

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


if __name__ == "__main__":
    app()
