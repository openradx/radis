#! /usr/bin/env python3
import json
import socket
import sys
import time
import uuid
from contextlib import nullcontext
from datetime import datetime, timezone
from datetime import time as time_of_day
from pathlib import Path
from random import randint
from typing import Annotated, Any, Literal

import openai
import requests
import typer
from adit_radis_shared.cli import commands
from adit_radis_shared.cli import helper as cli_helper
from faker import Faker
from openai.types.chat import ChatCompletion, ChatCompletionMessageParam
from pydicom.uid import generate_uid
from radis_client.client import RadisClient, ReportData

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


def parse_ddmmyyyy(value: str) -> datetime:
    try:
        return datetime.strptime(value, "%d%m%Y")
    except ValueError:
        raise typer.BadParameter("Date must be in format ddmmyyyy (e.g., 14022025).")


@app.command()
def generate_example_reports(
    ctx: typer.Context,
    group_id: Annotated[
        int,
        typer.Option(help="Group ID of the newly generated reports", show_default=False),
    ] = 1,
    out: Annotated[
        str | None,
        typer.Option(help="Write reports to this file instead of uploading", show_default=False),
    ] = None,
    overwrite: Annotated[bool, typer.Option(help="Overwrite existing file")] = False,
    count: Annotated[int, typer.Option(help="Number of reports to generate")] = 1,
    patient_id: Annotated[str | None, typer.Option(help="Patient ID")] = None,
    patient_birthdate: Annotated[
        datetime | None,
        typer.Option(
            help="Patient Birthdate (ddmmyyyy)",
            parser=parse_ddmmyyyy,
        ),
    ] = None,
    patient_sex: Annotated[
        Literal["M", "F", "O"] | None,
        typer.Option(help="Patient Sex (M, F, or O)"),
    ] = None,
    modality: Annotated[str | None, typer.Option(help="Modality")] = None,
    study_description: Annotated[str | None, typer.Option(help="Study Description")] = None,
    study_date: Annotated[
        datetime | None,
        typer.Option(
            help="Study Date (ddmmyyyy)",
            parser=parse_ddmmyyyy,
        ),
    ] = None,
    lng: Annotated[Literal["en", "de"] | None, typer.Option(help="Language (en or de)")] = "en",
    content: Annotated[
        str | None, typer.Option(help="Generates the report with the desired content")
    ] = None,
):
    """Generate example reports and either write them to disk or upload them via the API."""

    helper = cli_helper.CommandHelper()
    config = helper.load_config_from_env_file()

    # Check config values
    base_url = config.get("REPORT_LLM_PROVIDER_URL")
    if not base_url:
        sys.exit("Missing REPORT_LLM_PROVIDER_URL setting in .env file")

    api_key = config.get("REPORT_LLM_PROVIDER_API_KEY")
    if not api_key:
        sys.exit("Missing REPORT_LLM_PROVIDER_API_KEY setting in .env file")

    model = config.get("REPORT_LLM_MODEL_NAME")
    if not model:
        sys.exit("Missing REPORT_LLM_MODEL_NAME setting in .env file")

    out_path = Path(out) if out else None
    if out_path and out_path.exists() and not overwrite:
        sys.exit(f"File '{out_path.absolute()}' already exists.")

    auth_token: str | None
    api_online = False
    api_url: str | None = None
    radis_client: RadisClient | None = None
    faker: Faker

    # Check if API is online if no output path is provided
    if not out_path:
        if not helper.check_compose_up():
            sys.exit("Uploading reports via the API requires the dev containers running.")
        else:
            auth_token = config.get("SUPERUSER_AUTH_TOKEN")
            if not auth_token:
                sys.exit("Missing SUPERUSER_AUTH_TOKEN setting in .env file.")
            port = config.get("WEB_DEV_PORT")
            if not port:
                sys.exit("Missing WEB_DEV_PORT setting in .env file")
            api_url = f"http://localhost:{port}"

            radis_client = RadisClient(api_url, auth_token)
            api_online = True

    llm_client = openai.OpenAI(base_url=base_url, api_key=api_key)

    # Get the provided command parameters
    command = ctx.command
    if command is None:
        sys.exit("Unable to access command context")
    params: dict[str, Any] = ctx.params

    if study_date:
        params["study_date"] = datetime.combine(study_date, time_of_day(12, 0, tzinfo=timezone.utc))

    # Build prompt context from the command parameters
    context_lines = []  # All parameter values are given as context to the LLM except for below
    exclude = {"ctx", "group_id", "out", "overwrite", "count"}

    for meta_param in command.params:
        param_name = str(meta_param.name)
        param_help = getattr(meta_param, "help", None)
        param_value = params.get(param_name)
        if param_name not in exclude and param_value not in (None, "", False):
            context_lines.append(f"{param_help}: {str(param_value)}")

    system_prompt = """You are a radiologist. Write a radiology report. 
    If context is provided, follow all context variables when generating your report. 
    Output only the report text."""
    user_prompt = "Write the radiology report."
    context_block = "\n".join(context_lines)

    if context_block:
        user_prompt = f"{user_prompt}\n\nContext:\n{context_block}"

    messages: list[ChatCompletionMessageParam] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]

    print(f"Generating {count} example reports...")

    start = time.time()
    written_reports = 0
    upload_succeeded = 0
    upload_failed = 0
    reports_url: str | None = f"{api_url}/api/reports/" if api_url else None
    upload_message_printed = False

    if out_path:
        out_path.parent.mkdir(parents=True, exist_ok=True)

    with open(out_path, "w") if out_path else nullcontext(None) as file_handle:
        if file_handle is not None:
            file_handle.write("[\n")
        else:
            assert radis_client is not None
            assert reports_url is not None
            faker = Faker()

        for _ in range(count):
            response: ChatCompletion | None = None
            retries = 0
            while not response:
                try:
                    response = llm_client.chat.completions.create(
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
            print(".", end="", flush=True)

            # Write the generated report to file
            if file_handle is not None:
                if written_reports:
                    file_handle.write(",\n")
                file_handle.write("    ")
                json.dump(content, file_handle, ensure_ascii=False)
                file_handle.flush()
                written_reports += 1

            # Upload the generated report via the API
            elif radis_client is not None:
                if not upload_message_printed:
                    print("")
                    print(f"Uploading example report(s) to {reports_url}...")
                    upload_message_printed = True

                report_data = _create_report_data(content, params, faker)
                try:
                    radis_client.create_report(report_data)
                    upload_succeeded += 1
                except requests.HTTPError as err:
                    upload_failed += 1
                    print("x", end="", flush=True)
                    upload_response = err.response
                    status_code = upload_response.status_code if upload_response else "?"
                    response_text = upload_response.text if upload_response else str(err)
                    print(f"\nFailed to upload report: HTTP {status_code} - {response_text}")
                except Exception as err:  # pragma: no cover - defensive logging
                    upload_failed += 1
                    print("x", end="", flush=True)
                    print(f"\nFailed to upload report: {type(err).__name__}: {err}")
        print("")

        if file_handle is not None:
            if written_reports:
                file_handle.write("\n]\n")
            else:
                file_handle.write("]\n")

    duration = time.time() - start
    if out_path:
        print(f"Done in {duration:.2f}s")
        print(f"Example report(s) written to '{out_path.absolute()}'")
    elif api_online:
        assert reports_url is not None
        print(f"Done in {duration:.2f}s")
        print(f"Successfully Uploaded {upload_succeeded} example report(s) to '{reports_url}'")
        print(f"Failed Uploading {upload_failed} example report(s) to '{reports_url}'")
    else:
        sys.exit("No output path specified and API is not reachable.")


def _create_report_data(
    body: str,
    params: dict[str, Any],
    faker: Faker,
) -> ReportData:
    metadata = {
        "series_instance_uid": str(generate_uid()),
        "sop_instance_uid": str(generate_uid()),
    }

    return ReportData(
        document_id=str(uuid.uuid4()),
        language=params.get("lng") or "en",
        groups=[params.get("group_id") or 1],
        pacs_aet=faker.bothify("AE####").upper(),
        pacs_name=faker.company(),
        pacs_link=faker.url(),
        patient_id=params.get("patient_id") or faker.numerify("##########"),
        patient_birth_date=params.get("patient_birthdate")
        or faker.date_of_birth(minimum_age=25, maximum_age=90),
        patient_sex=params.get("patient_sex") or faker.random_element(elements=("M", "F", "O")),
        study_description=params.get("study_description") or faker.text(max_nb_chars=64),
        study_datetime=params.get("study_date")
        or faker.date_time_between(start_date="-5y", end_date="now", tzinfo=timezone.utc),
        study_instance_uid=generate_uid(),
        accession_number=faker.numerify("############"),
        modalities=[params.get("modality") or "CT"],
        metadata=metadata,
        body=body,
    )


if __name__ == "__main__":
    app()
