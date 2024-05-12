from argparse import ArgumentParser

from django.conf import settings
from django.core.management.base import BaseCommand

from radis.opensearch.client import get_client
from radis.opensearch.mappings import create_mappings


class Command(BaseCommand):
    help = "Setup OpenSearch index for reports."

    def add_arguments(self, parser: ArgumentParser) -> None:
        parser.add_argument(
            "--mappings",
            choices=["dev", "prod"],
            help="Create mappings for dev or prod (if those don't exist).",
        )

    def handle(self, *args, **options):
        client = get_client()

        if options["mappings"]:
            env = options["mappings"]
            if env == "dev":
                index_settings = {
                    "number_of_shards": 1,
                    "number_of_replicas": 0,
                }
            elif env == "prod":
                index_settings = {
                    "number_of_shards": 3,
                    "number_of_replicas": 1,
                }
            else:
                raise ValueError(f"Unknown environment: {env}")

            for index in settings.OPENSEARCH_INDICES:
                index_name = f"reports_{index['language']}"
                if not client.indices.exists(index=index_name):
                    result = client.indices.create(
                        index=index_name,
                        body={
                            "settings": index_settings,
                            "mappings": create_mappings(index["analyzer"]),
                        },
                    )
                    assert result["acknowledged"]
