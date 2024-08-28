from django.contrib.auth.models import Group
from django.core.management.base import BaseCommand, CommandParser

from .populate_example_reports import create_report


class Command(BaseCommand):
    help = "Add a custom report to the database."

    def add_arguments(self, parser: CommandParser) -> None:
        super().add_arguments(parser)

        parser.add_argument(
            "--report-body",
            type=str,
            help="The body of the report.",
        )
        parser.add_argument(
            "--report-language",
            default="en",
            help="The language of the report (en or de).",
        )

    def handle(self, *args, **options):
        group = Group.objects.first()

        if not group:
            raise ValueError("No group found. Please create a group.")

        create_report(options["report_body"], options["report_language"], group)
