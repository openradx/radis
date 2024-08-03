from typing import TYPE_CHECKING

from adit_radis_shared.accounts.models import Group
from adit_radis_shared.common.models import AppSettings
from django.conf import settings
from django.db import models
from procrastinate.contrib.django.models import ProcrastinateJob

import tasks
from radis.core.models import AnalysisJob, AnalysisTask
from radis.reports.models import Language, Modality, Report

if TYPE_CHECKING:
    from django.db.models.manager import RelatedManager


class InboxAppSettings(AppSettings):
    class Meta:
        verbose_name_plural = "Inbox app settings"


class Inbox(models.Model):
    title = models.CharField(max_length=100)
    group = models.ForeignKey(Group, on_delete=models.CASCADE)

    provider = models.CharField(max_length=100)
    query = models.CharField(max_length=200, null=True, blank=True)
    language = models.ForeignKey(Language, on_delete=models.CASCADE, blank=True, null=True)
    modalities = models.ManyToManyField(Modality, blank=True)
    study_date_from = models.DateField(null=True, blank=True)
    study_date_till = models.DateField(null=True, blank=True)
    study_description = models.CharField(max_length=200, blank=True)
    patient_sex = models.CharField(
        max_length=1, blank=True, choices=[("", "All"), ("M", "Male"), ("F", "Female")]
    )
    age_from = models.IntegerField(null=True, blank=True)
    age_till = models.IntegerField(null=True, blank=True)
    patient_id = models.CharField(max_length=100, null=True, blank=True)

    last_refreshed = models.DateTimeField(auto_now_add=True)

    if TYPE_CHECKING:
        items = RelatedManager["InboxItem"]()


class InboxItem(models.Model):
    inbox = models.ForeignKey(Inbox, on_delete=models.CASCADE)
    report = models.ForeignKey(Report, on_delete=models.CASCADE)


class RefreshInboxJob(AnalysisJob):
    default_priority = settings.INBOX_DEFAULT_PRIORITY
    urgent_priority = settings.INBOX_URGENT_PRIORITY
    continuous_job = True

    queued_job_id: int | None
    queued_job = models.OneToOneField(
        ProcrastinateJob, null=True, on_delete=models.SET_NULL, related_name="+"
    )

    if TYPE_CHECKING:
        tasks = RelatedManager["RefreshInboxTask"]()

    def __str__(self) -> str:
        return f"RefreshInboxJob {self.id}"

    def delay(self) -> None: ...


class RefreshInboxTask(AnalysisTask):
    job = models.ForeignKey(RefreshInboxJob, on_delete=models.CASCADE)

    def __str__(self) -> str:
        return f"RefreshInboxTask {self.id} for {self.inbox}"

    def delay(self) -> None: ...
