from typing import Any, ClassVar

from django.conf import settings
from django.db import models
from django.urls import reverse
from django.utils import timezone
from procrastinate.contrib.django import app
from procrastinate.contrib.django.models import ProcrastinateJob

from radis.core.models import AnalysisJob, AnalysisTask
from radis.reports.models import Report


def normalize_definition_text(value: str) -> str:
    """Canonical form of definition text, so that CRLF line endings (what browsers submit
    for a textarea) and surrounding whitespace never count as a definition change."""
    return value.replace("\r\n", "\n").replace("\r", "\n").strip()


def comparable_definition_value(value: object) -> object:
    """A definition value as compared for change detection: text normalized, anything else
    (a foreign key) as is."""
    return normalize_definition_text(value) if isinstance(value, str) else value


class DefinitionTrackedModel(models.Model):
    """Abstract base for Label and LabelGroup.

    Tracks when the definition the LLM depends on last changed. save() bumps
    `definition_updated_at` only if a field named in `DEFINITION_FIELDS` differs from the stored
    row (text is compared after normalization). Result staleness is measured against this field,
    not against the auto_now `updated_at`. Like auto_now, it is not maintained by
    QuerySet.update(), bulk_update() or loaddata.
    """

    DEFINITION_FIELDS: ClassVar[tuple[str, ...]] = ()
    definition_updated_at = models.DateTimeField(default=timezone.now, editable=False)

    class Meta:
        abstract = True

    def save(self, **kwargs: Any) -> None:
        update_fields = kwargs.get("update_fields")
        if update_fields is not None:
            update_fields = set(update_fields)
            kwargs["update_fields"] = update_fields
        attnames = self._definition_attnames_to_save(update_fields)
        self._normalize_definition(attnames)
        if self._definition_differs_from_stored(attnames):
            self.definition_updated_at = timezone.now()
            if update_fields is not None:
                update_fields.add("definition_updated_at")
        super().save(**kwargs)

    def _definition_attnames_to_save(self, update_fields: set[str] | None) -> tuple[str, ...]:
        """Column names of the definition fields this save persists."""
        attname_by_name = {f.name: f.attname for f in self._meta.concrete_fields}
        # A deferred field was never loaded, so it cannot have been edited; reading it here
        # would un-defer it and make Django write it back.
        deferred = self.get_deferred_fields() if update_fields is None else set()
        attnames = []
        for name in self.DEFINITION_FIELDS:
            attname = attname_by_name[name]
            if update_fields is not None and not {name, attname} & update_fields:
                continue
            if attname in deferred:
                continue
            attnames.append(attname)
        return tuple(attnames)

    def _normalize_definition(self, attnames: tuple[str, ...]) -> None:
        for attname in attnames:
            value = getattr(self, attname)
            if isinstance(value, str):
                setattr(self, attname, normalize_definition_text(value))

    def _definition_differs_from_stored(self, attnames: tuple[str, ...]) -> bool:
        if self.pk is None or not attnames:
            return False  # on add the field default already applies
        # _base_manager, like refresh_from_db: a filtering default manager must not hide the
        # row and make a real change go unrecorded.
        stored = (
            type(self)
            ._base_manager.using(self._state.db)
            .filter(pk=self.pk)
            .values_list(*attnames)
            .first()
        )
        if stored is None:
            return False
        return tuple(getattr(self, a) for a in attnames) != tuple(
            comparable_definition_value(v) for v in stored
        )


class LabelGroup(DefinitionTrackedModel):
    DEFINITION_FIELDS = ("name", "gate_question")  # name is the gate schema's field key

    id: int
    name = models.CharField(max_length=100, unique=True)
    gate_question = models.TextField()  # upfront Yes/No screening question for this group
    updated_at = models.DateTimeField(auto_now=True)  # audit only; see definition_updated_at

    labels: models.QuerySet["Label"]
    gate_answers: models.QuerySet["GateAnswer"]

    class Meta:
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name


class Label(DefinitionTrackedModel):
    # name is the classification schema's field key; the group decides which gate answer the
    # results were produced under.
    DEFINITION_FIELDS = ("group", "name", "description")

    id: int
    group_id: int
    group = models.ForeignKey(LabelGroup, on_delete=models.CASCADE, related_name="labels")
    name = models.CharField(max_length=100)  # the label string that surfaces (e.g. "pneumonia")
    description = models.TextField()  # definition sent to the LLM to classify this label
    active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)  # audit only; see definition_updated_at

    results: models.QuerySet["LabelResult"]

    class Meta:
        constraints = [models.UniqueConstraint(fields=["name"], name="unique_label_name")]
        indexes = [models.Index(fields=["active"])]

    def __str__(self) -> str:
        return self.name


class LabelResult(models.Model):
    class Value(models.TextChoices):
        PRESENT = "PRESENT", "Present"
        LIKELY = "LIKELY", "Likely"
        POSSIBLE = "POSSIBLE", "Possible"
        ABSENT = "ABSENT", "Absent"
        UNMENTIONED = "UNMENTIONED", "Unmentioned"

    # Buckets that attach the label to the report / search.
    SURFACING_VALUES = (Value.PRESENT, Value.LIKELY, Value.POSSIBLE)

    label_id: int
    report = models.ForeignKey(Report, on_delete=models.CASCADE, related_name="label_results")
    label = models.ForeignKey(Label, on_delete=models.CASCADE, related_name="results")
    value = models.CharField(max_length=11, choices=Value.choices)
    generated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["report", "label"], name="unique_result_per_report_label"
            ),
        ]
        indexes = [
            models.Index(fields=["label", "value"]),  # search facet lookups
            models.Index(fields=["report"]),  # report detail page render
        ]

    def __str__(self) -> str:
        return f"LabelResult {self.label_id}={self.value} [{self.pk}]"

    @property
    def is_stale(self) -> bool:
        """Stale when the label's definition or the report's content changed after generation."""
        return (
            self.generated_at < self.label.definition_updated_at
            or self.generated_at < self.report.updated_at
        )


class GateAnswer(models.Model):
    class Value(models.TextChoices):
        YES = "YES", "Yes"
        NO = "NO", "No"

    label_group_id: int
    report = models.ForeignKey(Report, on_delete=models.CASCADE, related_name="gate_answers")
    label_group = models.ForeignKey(
        LabelGroup, on_delete=models.CASCADE, related_name="gate_answers"
    )
    value = models.CharField(max_length=3, choices=Value.choices)
    generated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["report", "label_group"], name="unique_gate_answer_per_report_group"
            ),
        ]
        indexes = [models.Index(fields=["label_group", "value"])]

    def __str__(self) -> str:
        return f"GateAnswer {self.label_group_id}={self.value} [{self.pk}]"

    @property
    def is_stale(self) -> bool:
        """Stale when the group's definition (name or gate question) or the report's content
        changed after generation."""
        return (
            self.generated_at < self.label_group.definition_updated_at
            or self.generated_at < self.report.updated_at
        )


class LabelingScanCheckpoint(models.Model):
    last_scanned_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = "Labeling scan checkpoint"
        constraints = [
            models.CheckConstraint(
                condition=models.Q(id=1), name="singleton_labeling_scan_checkpoint"
            ),
        ]

    def __str__(self) -> str:
        return f"LabelingScanCheckpoint (last_scanned_at={self.last_scanned_at})"

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)


class LabelingJob(AnalysisJob):
    class Trigger(models.TextChoices):
        SCAN = "SCAN", "Periodic scan"
        MANUAL = "MANUAL", "Manual backfill"

    default_priority = settings.LABELING_JOB_PRIORITY
    urgent_priority = settings.LABELING_JOB_PRIORITY  # labeling is never urgent

    # Scan jobs have no human owner; override the non-nullable base FK to allow null.
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="%(app_label)s_jobs",
    )
    queued_job_id: int | None
    queued_job = models.OneToOneField(
        ProcrastinateJob, null=True, on_delete=models.SET_NULL, related_name="+"
    )
    trigger = models.CharField(max_length=10, choices=Trigger.choices, default=Trigger.MANUAL)
    scan_from = models.DateTimeField(null=True, blank=True)

    tasks: models.QuerySet["LabelingTask"]

    ACTIVE_STATUSES = (
        AnalysisJob.Status.UNVERIFIED,
        AnalysisJob.Status.PREPARING,
        AnalysisJob.Status.PENDING,
        AnalysisJob.Status.IN_PROGRESS,
        AnalysisJob.Status.CANCELING,
    )

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"LabelingJob [{self.pk}]"

    def get_absolute_url(self) -> str:
        return reverse("admin:labels_labelingjob_change", args=[self.pk])

    def delay(self) -> None:
        queued_job_id = app.configure_task(
            "radis.labels.tasks.process_labeling_job",
            allow_unknown=False,
            priority=self.default_priority,
        ).defer(job_id=self.pk)
        self.queued_job_id = queued_job_id
        self.save()

    # No completion mail in v1 (scan jobs are owner-less; no labeling mail template exists).
    finished_mail_template = None

    def _send_job_finished_mail(self) -> None:
        return


class LabelingTask(AnalysisTask):
    job = models.ForeignKey(LabelingJob, on_delete=models.CASCADE, related_name="tasks")
    reports = models.ManyToManyField(Report, related_name="+")

    def get_absolute_url(self) -> str:
        return reverse("admin:labels_labelingtask_change", args=[self.pk])

    def delay(self) -> None:
        queued_job_id = app.configure_task(
            "radis.labels.tasks.process_labeling_task",
            allow_unknown=False,
            priority=self.job.default_priority,
        ).defer(task_id=self.pk)
        self.queued_job_id = queued_job_id
        self.save()
