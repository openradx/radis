from django.core.management.base import BaseCommand
from django.db import transaction

from radis.reports.models import Report


class Command(BaseCommand):
    help = "Update search_vector for existing Report records"

    def handle(self, *args, **kwargs):
        with transaction.atomic():
            reports = Report.objects.all()
            for report in reports:
                report.update_search_vector()
                report.save()
        self.stdout.write(self.style.SUCCESS("Successfully updated search_vector for all reports"))
