import json
import time
from pathlib import Path
from random import randint
from typing import Annotated

import openai
from adit_radis_shared.maintenance_commands.management.base.maintenance_command import (
    MaintenanceCommand,
)
from openai.types.chat import ChatCompletion, ChatCompletionMessageParam
from typer import Exit, Option

SYSTEM_PROMPT = {
    "de": "Du bist ein Radiologe.",
    "en": "You are a radiologist.",
}

USER_PROMPT = {
    "de": "Schreibe einen radiologischen Befund.",
    "en": "Write a radiology report.",
}


class Command(MaintenanceCommand):
    """Start stack with docker compose"""

    def handle(
        self,
        out: Annotated[str, Option(help="Output file")] = "example_reports.json",
        count: Annotated[int, Option(help="Number of reports to generate")] = 10,
        model: Annotated[str, Option(help="OpenAI model")] = "gpt-3.5-turbo",
        lng: Annotated[str, Option(help="Language")] = "en",
        overwrite: Annotated[bool, Option(help="Overwrite existing file")] = False,
    ):
        """Generate example reports"""
        print(f"Generating {count} example reports...")

        config = self.load_config_from_env_file()

        openai_api_key = config.get("OPENAI_API_KEY")
        if not openai_api_key:
            print("Missing OPENAI_API_KEY setting in .env file")
            raise Exit(1)

        out_path = Path(out)
        if out_path.exists() and not overwrite:
            print(f"File '{out_path.absolute()}' already exists.")
            raise Exit(1)

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
