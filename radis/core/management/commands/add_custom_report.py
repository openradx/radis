from django.contrib.auth.models import Group
from django.core.management.base import BaseCommand, CommandParser

from radis.reports.factories import LanguageFactory, ReportFactory


class Command(BaseCommand):
    help = "Add a custom report to the database."

    def add_arguments(self, parser: CommandParser) -> None:
        super().add_arguments(parser)

        parser.add_argument("--body", type=str, help="The body of the report.")
        parser.add_argument(
            "--lng",
            default="en",
            help="The language of the report ('en' or 'de'). Defaults to 'en'.",
        )
        parser.add_argument(
            "--group",
            type=int,
            default=None,
            help="The group to assign the report to. "
            "If not set then the first group in the database will be used.",
        )

    def handle(self, *args, **options):
        if options["group"] is not None:
            try:
                group = Group.objects.get(id=options["group"])
            except Group.DoesNotExist:
                raise ValueError(f"Group with ID {options['group']} does not exist.")
        else:
            group = Group.objects.order_by("id").first()
            if group is None:
                raise ValueError("No group found. Please create a group first.")

        body = options["body"]
        language = options["lng"]

        self.stdout.write(
            f"Adding custom report to group '{group.name}' into the database...", ending=""
        )
        self.stdout.flush()

        report = ReportFactory.create(language=LanguageFactory(code=language), body=body)
        report.groups.set([group])

        self.stdout.write("Done")
