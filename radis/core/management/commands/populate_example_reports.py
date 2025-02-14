import json
import time
from pathlib import Path

from django.conf import settings
from django.contrib.auth.models import Group
from django.core.management.base import BaseCommand, CommandParser
from faker import Faker

from radis.reports.factories import LanguageFactory, ReportFactory

fake = Faker()


class Command(BaseCommand):
    help = "Populates the database with example data."

    def add_arguments(self, parser: CommandParser) -> None:
        super().add_arguments(parser)

        parser.add_argument(
            "--group",
            type=int,
            default=None,
            help="The group to assign the reports to. "
            "If not set then the first group in the database will be used.",
        )
        parser.add_argument(
            "--lng",
            default="en",
            help="Which report language to use ('en' or 'de'). Defaults to 'en'.",
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

        language = options["lng"]
        if language == "en":
            sample_file = "reports_en.json"
        elif language == "de":
            sample_file = "reports_de.json"
        else:
            raise ValueError(f"Language {language} is not supported.")

        samples_path = Path(settings.BASE_PATH / "samples" / sample_file)
        with open(samples_path, "r") as f:
            report_bodies = json.load(f)

        self.stdout.write(
            f"Adding {len(report_bodies)} example reports "
            f"to group '{group.name}' into the database...",
            ending="",
        )
        self.stdout.flush()

        start = time.time()
        for report_body in report_bodies:
            report = ReportFactory.create(language=LanguageFactory(code=language), body=report_body)
            report.groups.set([group])

        self.stdout.write(f"Done (in {time.time() - start:.2f} seconds)")
