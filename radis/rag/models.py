from typing import TYPE_CHECKING, Callable

from celery import current_app
from django.conf import settings
from django.contrib.postgres.fields import ArrayField
from django.db import models
from django.urls import reverse
from django.utils.functional import lazy

from radis.core.models import AnalysisJob, AnalysisTask, AppSettings
from radis.reports.models import Report

from .site import retrieval_providers

if TYPE_CHECKING:
    from django.db.models.manager import RelatedManager


def get_retrieval_providers():
    return sorted([(provider.name, provider.name) for provider in retrieval_providers.values()])


def get_default_provider():
    providers = get_retrieval_providers()
    if providers:
        return providers[0][0]


# TODO: Rename to RagSettings (as in ADIT)
class RagAppSettings(AppSettings):
    class Meta:
        verbose_name_plural = "RAG app settings"


class RagJob(AnalysisJob):
    default_priority = settings.RAG_DEFAULT_PRIORITY
    urgent_priority = settings.RAG_URGENT_PRIORITY
    continuous_job = False

    title = models.CharField(max_length=100)
    "The title of the job that is shown in the job list"

    # Search and filter fields
    # TODO: in Django 5 choices can be passed a function directly, too
    provider = models.CharField(
        max_length=100, choices=lazy(get_retrieval_providers, tuple)(), default=get_default_provider
    )
    query = models.CharField(max_length=200)
    study_date_from = models.DateField(null=True, blank=True)
    study_date_till = models.DateField(null=True, blank=True)
    study_description = models.CharField(max_length=200, blank=True)
    modalities = ArrayField(models.CharField(max_length=16))
    patient_sex = models.CharField(
        max_length=1, blank=True, choices=[("", "All"), ("M", "Male"), ("F", "Female")]
    )
    age_from = models.IntegerField(null=True, blank=True)
    age_till = models.IntegerField(null=True, blank=True)

    if TYPE_CHECKING:
        tasks = RelatedManager["RagTask"]()
        questions = RelatedManager["Question"]()

    def __str__(self) -> str:
        return f"RagJob {self.id}"

    def get_absolute_url(self) -> str:
        return reverse("rag_job_detail", args=[self.id])

    def delay(self) -> None:
        current_app.send_task("radis.rag.tasks.ProcessRagJob", args=[self.id])


class Answer(models.TextChoices):
    YES = "Y", "Yes"
    NO = "N", "No"


class Question(models.Model):
    id: int
    job = models.ForeignKey(RagJob, on_delete=models.CASCADE, related_name="questions")
    question = models.CharField(max_length=500)
    accepted_answer = models.CharField(max_length=1, choices=Answer.choices, default=Answer.YES)
    get_accepted_answer_display: Callable[[], str]

    def __str__(self) -> str:
        return f'Question: "{self.question}"'


class RagTask(AnalysisTask):
    class Result(models.TextChoices):
        ACCEPTED = "A", "Accepted"
        REJECTED = "R", "Rejected"

    if TYPE_CHECKING:
        results = RelatedManager["QuestionResult"]()

    job = models.ForeignKey(RagJob, on_delete=models.CASCADE, related_name="tasks")
    report = models.ForeignKey(Report, on_delete=models.CASCADE, related_name="rag_tasks")
    overall_result = models.CharField(max_length=1, choices=Result.choices, blank=True)

    def get_absolute_url(self) -> str:
        return reverse("rag_task_detail", args=[self.id])

    def delay(self) -> None:
        current_app.send_task("radis.rag.tasks.ProcessRagTask", args=[self.id])


class QuestionResult(models.Model):
    id: int
    task = models.ForeignKey(RagTask, on_delete=models.CASCADE, related_name="results")
    question = models.ForeignKey(Question, on_delete=models.CASCADE, related_name="results")
    answer = models.CharField(max_length=1, choices=Answer.choices)
    get_answer_display: Callable[[], str]
    result = models.CharField(max_length=1, choices=RagTask.Result.choices)

    def __str__(self) -> str:
        return f'Result of "{self.question.question}"'
