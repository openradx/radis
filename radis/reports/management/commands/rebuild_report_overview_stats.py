from django.contrib.auth.models import Group
from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models import Count
from django.db.models.functions import TruncYear

from radis.reports.models import (
    Report,
    ReportLanguageStat,
    ReportModalityStat,
    ReportOverviewTotal,
    ReportYearStat,
)


class Command(BaseCommand):
    help = "Rebuild per-group report overview statistics."

    def handle(self, *args, **options):
        groups = Group.objects.all().only("id")
        for group in groups:
            report_qs = Report.objects.filter(groups=group)
            total_count = report_qs.count()

            year_counts = (
                report_qs.annotate(year=TruncYear("study_datetime"))
                .values("year")
                .annotate(count=Count("id"))
                .order_by()
            )
            modality_counts = (
                report_qs.values("modalities__code")
                .annotate(count=Count("id", distinct=True))
                .order_by()
            )
            language_counts = (
                report_qs.values("language__code").annotate(count=Count("id")).order_by()
            )

            year_stats = [
                ReportYearStat(group=group, year=item["year"].year, count=item["count"])
                for item in year_counts
                if item["year"]
            ]
            modality_stats = [
                ReportModalityStat(
                    group=group,
                    modality_code=item["modalities__code"] or "Unknown",
                    count=item["count"],
                )
                for item in modality_counts
            ]
            language_stats = [
                ReportLanguageStat(
                    group=group,
                    language_code=item["language__code"] or "Unknown",
                    count=item["count"],
                )
                for item in language_counts
            ]

            with transaction.atomic():
                ReportOverviewTotal.objects.update_or_create(
                    group=group, defaults={"total_count": total_count}
                )
                ReportYearStat.objects.filter(group=group).delete()
                ReportModalityStat.objects.filter(group=group).delete()
                ReportLanguageStat.objects.filter(group=group).delete()
                ReportYearStat.objects.bulk_create(year_stats)
                ReportModalityStat.objects.bulk_create(modality_stats)
                ReportLanguageStat.objects.bulk_create(language_stats)

        self.stdout.write(self.style.SUCCESS("Overview stats rebuilt."))
