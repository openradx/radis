#! /usr/bin/env python3
# PYTHON_ARGCOMPLETE_OK
import argparse
import json
import socket
import sys
import time
from pathlib import Path
from random import randint

import argcomplete
import openai
from adit_radis_shared.cli import commands, parsers
from adit_radis_shared.cli import helper as cli_helper
from openai.types.chat import ChatCompletion, ChatCompletionMessageParam


def compose_up(build: bool, profile: list[str], extra_args: list[str], **kwargs):
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

    commands.compose_up(build=build, profile=profiles, extra_args=extra_args, **kwargs)


def compose_down(cleanup: bool, profile: list[str], **kwargs):
    helper = cli_helper.CommandHelper()

    config = helper.load_config_from_env_file()
    if str(config.get("GPU_INFERENCE_ENABLED", "")).lower() in ["yes", "true", "1"]:
        profiles = profile + ["gpu"] if "gpu" not in profile else profile
    else:
        profiles = profile + ["cpu"] if "cpu" not in profile else profile

    commands.compose_down(cleanup=cleanup, profile=profiles, **kwargs)


def get_host_ip(**kwargs):
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


def generate_example_reports(out: str, count: int, model: str, lng: str, overwrite: bool):
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
    root_parser = argparse.ArgumentParser()
    subparsers = root_parser.add_subparsers(dest="command")

    parsers.register_compose_up(subparsers, func=compose_up)
    parsers.register_compose_down(subparsers, func=compose_down)
    parsers.register_db_backup(subparsers)
    parsers.register_db_restore(subparsers)
    parsers.register_format_code(subparsers)
    parsers.register_generate_auth_token(subparsers)
    parsers.register_generate_certificate_chain(subparsers)
    parsers.register_generate_certificate_files(subparsers)
    parsers.register_generate_django_secret_key(subparsers)
    parsers.register_generate_secure_password(subparsers)
    parsers.register_init_workspace(subparsers)
    parsers.register_lint(subparsers)
    parsers.register_randomize_env_secrets(subparsers)
    parsers.register_shell(subparsers)
    parsers.register_show_outdated(subparsers)
    parsers.register_stack_deploy(subparsers)
    parsers.register_stack_rm(subparsers)
    parsers.register_test(subparsers)
    parsers.register_try_github_actions(subparsers)
    parsers.register_upgrade_postgres_volume(subparsers)

    info = "Get the IP of the Docker host"
    parser = subparsers.add_parser("get_host_ip", help=info, description=info)
    parser.set_defaults(func=get_host_ip)

    info = "Generate example reports"
    parser = subparsers.add_parser("generate_example_reports", help=info, description=info)
    parser.add_argument("--out", type=str, default="example_reports.json", help="Output file")
    parser.add_argument("--count", type=int, default=10, help="Number of reports to generate")
    parser.add_argument(
        "--model", type=str, default="gpt-3.5-turbo", help="The OpenAI model to use"
    )
    parser.add_argument("--lng", type=str, default="en", help="Language generated rerports (de/en)")
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Overwrite existing file",
    )
    parser.set_defaults(func=generate_example_reports)

    argcomplete.autocomplete(root_parser)
    args, extra_args = root_parser.parse_known_args()
    if not args.command:
        root_parser.print_help()
        sys.exit(1)

    args.func(**vars(args), extra_args=extra_args)
