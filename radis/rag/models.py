from typing import Callable

from adit_radis_shared.common.models import AppSettings
from django.conf import settings
from django.contrib.auth.models import Group
from django.db import models
from django.urls import reverse
from procrastinate.contrib.django import app
from procrastinate.contrib.django.models import ProcrastinateJob

from radis.core.models import AnalysisJob, AnalysisTask
from radis.reports.models import Language, Modality, Report


# TODO: Rename to RagSettings (as in ADIT)
class RagAppSettings(AppSettings):
    class Meta:
        verbose_name_plural = "RAG app settings"


class RagJob(AnalysisJob):
    default_priority = settings.RAG_DEFAULT_PRIORITY
    urgent_priority = settings.RAG_URGENT_PRIORITY
    continuous_job = False
    finished_mail_template = "rag/mail/finished_mail.html"

    queued_job_id: int | None
    queued_job = models.OneToOneField(
        ProcrastinateJob, null=True, on_delete=models.SET_NULL, related_name="+"
    )
    title = models.CharField(max_length=100)
    provider = models.CharField(max_length=100)
    group = models.ForeignKey(Group, on_delete=models.CASCADE)
    query = models.CharField(max_length=200)
    language = models.ForeignKey(Language, on_delete=models.CASCADE)
    modalities = models.ManyToManyField(Modality, blank=True)
    study_date_from = models.DateField(null=True, blank=True)
    study_date_till = models.DateField(null=True, blank=True)
    study_description = models.CharField(max_length=200, blank=True)
    patient_sex = models.CharField(
        max_length=1, blank=True, choices=[("", "All"), ("M", "Male"), ("F", "Female")]
    )
    age_from = models.IntegerField(null=True, blank=True)
    age_till = models.IntegerField(null=True, blank=True)

    tasks: models.QuerySet["RagTask"]
    questions: models.QuerySet["Question"]

    def __str__(self) -> str:
        return f"RagJob [{self.pk}]"

    def get_absolute_url(self) -> str:
        return reverse("rag_job_detail", args=[self.pk])

    def delay(self) -> None:
        queued_job_id = app.configure_task(
            "radis.rag.tasks.process_rag_job",
            allow_unknown=False,
            priority=self.urgent_priority if self.urgent else self.default_priority,
        ).defer(job_id=self.pk)
        self.queued_job_id = queued_job_id
        self.save()


class Answer(models.TextChoices):
    YES = "Y", "Yes"
    NO = "N", "No"


class Question(models.Model):
    question = models.CharField(max_length=500)
    job = models.ForeignKey(RagJob, on_delete=models.CASCADE, related_name="questions")
    accepted_answer = models.CharField(max_length=1, choices=Answer.choices, default=Answer.YES)
    get_accepted_answer_display: Callable[[], str]

    def __str__(self) -> str:
        return f'Question "{self.question}" [{self.pk}]'


class RagTask(AnalysisTask):
    job = models.ForeignKey(RagJob, on_delete=models.CASCADE, related_name="tasks")

    rag_instances: models.QuerySet["RagInstance"]

    def get_absolute_url(self) -> str:
        return reverse("rag_task_detail", args=[self.pk])

    def delay(self) -> None:
        queued_job_id = app.configure_task(
            "radis.rag.tasks.process_rag_task",
            allow_unknown=False,
            priority=self.job.urgent_priority if self.job.urgent else self.job.default_priority,
        ).defer(task_id=self.pk)
        self.queued_job_id = queued_job_id
        self.save()


class RagInstance(models.Model):
    class Result(models.TextChoices):
        ACCEPTED = "A", "Accepted"
        REJECTED = "R", "Rejected"

    text = models.TextField()
    report_id: int
    report = models.ForeignKey(Report, on_delete=models.CASCADE, related_name="rag_instances")
    other_reports = models.ManyToManyField(Report)
    overall_result = models.CharField(max_length=1, choices=Result.choices, blank=True)
    get_overall_result_display: Callable[[], str]
    task = models.ForeignKey(RagTask, on_delete=models.CASCADE, related_name="rag_instances")

    results: models.QuerySet["QuestionResult"]

    def __str__(self) -> str:
        return f"RagInstance [{self.pk}]"

    def get_absolute_url(self) -> str:
        return reverse("rag_instance_detail", args=[self.task.pk, self.pk])


class QuestionResult(models.Model):
    rag_instance = models.ForeignKey(RagInstance, on_delete=models.CASCADE, related_name="results")
    question = models.ForeignKey(Question, on_delete=models.CASCADE, related_name="results")
    original_answer = models.CharField(max_length=1, choices=Answer.choices)
    current_answer = models.CharField(max_length=1, choices=Answer.choices)
    get_current_answer_display: Callable[[], str]
    result = models.CharField(max_length=1, choices=RagInstance.Result.choices)

    def __str__(self) -> str:
        return f'Result of "{self.question}": {self.get_current_answer_display} [{self.pk}]'
