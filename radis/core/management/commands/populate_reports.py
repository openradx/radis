import json
import time
from pathlib import Path
from typing import Literal

from django.conf import settings
from django.contrib.auth.models import Group
from django.core.management.base import BaseCommand, CommandParser
from django.db import transaction
from faker import Faker

from radis.reports.factories import LanguageFactory, ReportFactory
from radis.reports.models import Report
from radis.reports.signals import report_signal_processor
from radis.reports.site import reports_created_handlers

fake = Faker()


def create_report(body: str, language: Literal["en", "de"], group: Group):
    report = ReportFactory.create(language=LanguageFactory(code=language), body=body)
    report.groups.set([group])
    return report


def create_reports(language: Literal["en", "de"], group: Group):
    if language == "en":
        sample_file = "reports_en.json"
    elif language == "de":
        sample_file = "reports_de.json"
    else:
        raise ValueError(f"Language {language} is not supported.")

    samples_path = Path(settings.BASE_DIR / "samples" / sample_file)
    with open(samples_path, "r") as f:
        report_bodies = json.load(f)

    report_signal_processor.pause()

    start = time.time()
    reports: list[Report] = []
    for report_body in report_bodies:
        reports.append(create_report(report_body, language, group))
    print(f"Generated {len(reports)} reports in {time.time() - start:.2f} seconds.")

    def create_documents() -> None:
        for handler in reports_created_handlers:
            start = time.time()
            handler.handle([report.id for report in reports])
            print(
                f"Fed {len(reports)} reports to {handler.name} in "
                f"{time.time() - start:.2f} seconds."
            )

    transaction.on_commit(create_documents)

    report_signal_processor.resume()


class Command(BaseCommand):
    help = "Populates the database with example data."

    def add_arguments(self, parser: CommandParser) -> None:
        super().add_arguments(parser)

        parser.add_argument(
            "--skip-reports",
            action="store_true",
            help="Skip populating the database with example reports.",
        )
        parser.add_argument(
            "--report-language",
            default="en",
            help="Which report language to use (en or de).",
        )

    def handle(self, *args, **options):
        group = Group.objects.first()

        if not group:
            raise ValueError("No group found. Please create a group.")

        if not options["skip_reports"]:
            if Report.objects.first():
                print("Reports already populated. Skipping.")
            else:
                print("Populating database with example reports.")
                create_reports(options["report_language"], group)
