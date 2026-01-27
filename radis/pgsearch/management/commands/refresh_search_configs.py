from django.core.management.base import BaseCommand

from radis.pgsearch.utils.language_utils import clear_search_config_cache


class Command(BaseCommand):
    help = "Clear cached PostgreSQL text search configs."

    def handle(self, *args, **options) -> None:
        clear_search_config_cache()
        self.stdout.write("Cleared cached text search configs.")
