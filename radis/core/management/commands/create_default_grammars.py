import time

from django.conf import settings
from django.core.management.base import BaseCommand, CommandParser
from faker import Faker

from radis.chats.models import Grammar

fake = Faker()


class Command(BaseCommand):
    help = "Populates the database with default grammars."

    def add_arguments(self, parser: CommandParser) -> None:
        super().add_arguments(parser)

    def handle(self, *args, **options):
        self.stdout.write(
            f"Adding {len(settings.CHAT_DEFAULT_GRAMMARS)} default grammars to the database...",
            ending="",
        )
        self.stdout.flush()

        start = time.time()
        for default_grammar in settings.CHAT_DEFAULT_GRAMMARS:
            default_grammar["is_default"] = True
            Grammar.objects.update_or_create(name=default_grammar["name"], defaults=default_grammar)
        self.stdout.write(f"Done (in {time.time() - start:.2f} seconds)")
