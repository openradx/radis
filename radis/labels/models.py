from __future__ import annotations

from typing import Callable

from django.conf import settings
from django.db import models
from django.db.models import Count, QuerySet

from radis.reports.models import Report


class QuestionSet(models.Model):
    """A coherent group of related questions that get answered together per report.

    Was previously called ``LabelGroup``. The rename keeps the data model
    consistent with how staff think and talk about it: a set of questions you
    pose to a radiology report, not a "group of labels".
    """

    id: int
    name = models.CharField(max_length=100)
    description = models.TextField(blank=True, default="")
    is_active = models.BooleanField(default=True)
    order = models.PositiveSmallIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    last_edited_at = models.DateTimeField(null=True, blank=True)

    questions: models.QuerySet["Question"]
    backfill_jobs: models.QuerySet["BackfillJob"]

    class Meta:
        ordering = ["order", "name"]

    def __str__(self) -> str:
        return f"QuestionSet {self.name} [{self.pk}]"

    @property
    def is_locked(self) -> bool:
        """True iff an active backfill exists for this set.

        While locked, staff edits to the set, its questions, or their answer
        options should be rejected. We keep this property purely DB-derived so
        no separate "locked" flag can ever drift from the actual backfill state.
        """
        return self.backfill_jobs.filter(
            status__in=[BackfillJob.Status.PENDING, BackfillJob.Status.IN_PROGRESS]
        ).exists()

    def missing_reports(self) -> QuerySet[Report]:
        """Reports that lack an answer for at least one (active question, mode) pair.

        A report is "complete" for this set iff, for every active question and
        every mode in ``settings.LABELS_RUN_MODES``, there is at least one
        ``Answer`` written by a SUCCESS ``LabelingRun`` for this set. Coverage
        is computed per question rather than per-run so that adding a new
        question to a set with prior runs correctly resurfaces every report
        as missing for the new question.

        Returns an empty queryset when the set has no active questions, since
        there is no work to dispatch in that case.
        """
        active_q_ids = list(
            self.questions.filter(is_active=True).values_list("id", flat=True)
        )
        if not active_q_ids:
            return Report.objects.none()

        modes = getattr(settings, "LABELS_RUN_MODES", [LabelingRun.Mode.DIRECT])
        if not modes:
            return Report.objects.none()

        active_q_count = len(active_q_ids)

        # Intersect "reports complete for mode" across all required modes. The
        # subquery groups Answer rows by report, counts distinct active
        # questions covered by SUCCESS runs in this mode, and keeps reports
        # whose coverage hits the required count.
        complete: QuerySet[Report] = Report.objects.all()
        for mode in modes:
            complete_for_mode = (
                Answer.objects.filter(
                    question_id__in=active_q_ids,
                    run__question_set=self,
                    run__mode=mode,
                    run__status=LabelingRun.Status.SUCCESS,
                )
                .values("report_id")
                .annotate(covered=Count("question_id", distinct=True))
                .filter(covered__gte=active_q_count)
                .values("report_id")
            )
            complete = complete.filter(id__in=complete_for_mode)

        return Report.objects.exclude(id__in=complete.values("id"))


class Question(models.Model):
    """A single question staff want answered about a report.

    ``version`` is incremented when the question text or its allowed answer
    options change semantically. ``Answer`` rows snapshot the version they were
    generated under so old answers remain attributable to the prompt that
    produced them. Auto-bump logic lives in :meth:`bump_version`, called by
    the form/save paths; we do *not* bump on cosmetic changes (order, etc.).
    """

    id: int
    question_set_id: int
    question_set = models.ForeignKey[QuestionSet](
        QuestionSet, on_delete=models.CASCADE, related_name="questions"
    )
    label = models.CharField(max_length=200)
    question = models.CharField(max_length=300, blank=True, default="")
    is_active = models.BooleanField(default=True)
    order = models.PositiveSmallIntegerField(default=0)
    version = models.PositiveIntegerField(default=1)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    options: models.QuerySet["AnswerOption"]

    class Meta:
        ordering = ["order", "label"]
        constraints = [
            models.UniqueConstraint(
                fields=["question_set", "label"],
                name="unique_question_label_per_set",
            )
        ]

    def __str__(self) -> str:
        return f"Question {self.label} [{self.pk}]"

    def save(self, *args, **kwargs) -> None:
        if not self.question:
            self.question = self.label
        super().save(*args, **kwargs)

    def bump_version(self) -> None:
        """Increment ``version`` atomically and persist.

        Use this whenever the prompt semantics change. Callers should follow
        up with a backfill so older reports get relabelled under the new
        version.
        """
        Question.objects.filter(pk=self.pk).update(version=models.F("version") + 1)
        self.refresh_from_db(fields=["version"])


class AnswerOption(models.Model):
    """One allowed answer value for a ``Question``.

    Was previously called ``LabelChoice``. The rename pairs cleanly with
    ``Question``/``Answer`` so the data model reads as a questionnaire.
    """

    id: int
    question_id: int
    question = models.ForeignKey[Question](
        Question, on_delete=models.CASCADE, related_name="options"
    )
    value = models.CharField(max_length=50)
    label = models.CharField(max_length=100)
    is_unknown = models.BooleanField(default=False)
    order = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ["order", "label"]
        constraints = [
            models.UniqueConstraint(
                fields=["question", "value"],
                name="unique_answer_option_value_per_question",
            )
        ]

    def __str__(self) -> str:
        return f"AnswerOption {self.label} [{self.pk}]"


class LabelingRun(models.Model):
    """One LLM exchange that produced answers for (report, question_set, mode).

    Existence of a SUCCESS run for each mode in ``settings.LABELS_RUN_MODES``
    is the criterion for a report being "complete" for a question set. The
    raw response and reasoning text are kept so evaluation can compare modes
    after the fact without re-querying the LLM.

    Multiple runs may exist per (report, question_set, mode) — re-runs after
    a question version bump produce new rows; eval and report-detail prefer
    the latest SUCCESS row by ``created_at``.
    """

    class Mode(models.TextChoices):
        DIRECT = "DI", "Direct"
        REASONED = "RE", "Reasoned"

    class Status(models.TextChoices):
        PENDING = "PE", "Pending"
        IN_PROGRESS = "IP", "In Progress"
        SUCCESS = "SU", "Success"
        FAILURE = "FA", "Failure"

    id: int
    report_id: int
    question_set_id: int
    report = models.ForeignKey[Report](
        Report, on_delete=models.CASCADE, related_name="labeling_runs"
    )
    question_set = models.ForeignKey[QuestionSet](
        QuestionSet, on_delete=models.CASCADE, related_name="labeling_runs"
    )
    mode = models.CharField(max_length=2, choices=Mode.choices)
    status = models.CharField(max_length=2, choices=Status.choices, default=Status.PENDING)
    model_name = models.CharField(max_length=200, blank=True, default="")
    reasoning_text = models.TextField(blank=True, default="")
    raw_response = models.JSONField(null=True, blank=True)
    error_message = models.TextField(blank=True, default="")
    prompt_tokens = models.PositiveIntegerField(null=True, blank=True)
    completion_tokens = models.PositiveIntegerField(null=True, blank=True)
    latency_ms = models.PositiveIntegerField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    get_mode_display: Callable[[], str]
    get_status_display: Callable[[], str]
    answers: models.QuerySet["Answer"]

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["report", "question_set", "mode", "status"]),
            models.Index(fields=["question_set", "mode", "status"]),
        ]

    def __str__(self) -> str:
        return f"LabelingRun report={self.report_id} mode={self.mode} [{self.pk}]"


class Answer(models.Model):
    """The chosen answer option for one ``Question`` in one ``LabelingRun``.

    Was previously ``ReportLabel``. We denormalize ``report`` here even though
    it is derivable from ``run.report`` because the report-detail page and
    ``QuestionSet.missing_reports`` both filter on report directly; the
    denormalization keeps those queries fast and indexable.

    ``question_version`` snapshots ``Question.version`` at write time so the
    answer remains attributable to the exact prompt that produced it even
    after the question is revised.
    """

    id: int
    run_id: int
    report_id: int
    question_id: int
    option_id: int
    run = models.ForeignKey[LabelingRun](
        LabelingRun, on_delete=models.CASCADE, related_name="answers"
    )
    report = models.ForeignKey[Report](Report, on_delete=models.CASCADE, related_name="answers")
    question = models.ForeignKey[Question](
        Question, on_delete=models.CASCADE, related_name="answers"
    )
    question_version = models.PositiveIntegerField()
    option = models.ForeignKey[AnswerOption](
        AnswerOption, on_delete=models.PROTECT, related_name="answers"
    )
    confidence = models.FloatField(null=True, blank=True)
    rationale = models.TextField(blank=True, default="")
    verified = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["run", "question"],
                name="unique_answer_per_run_question",
            )
        ]
        indexes = [
            models.Index(fields=["report", "question"]),
            models.Index(fields=["question", "question_version"]),
        ]

    def __str__(self) -> str:
        return f"Answer report={self.report_id} question={self.question_id} [{self.pk}]"


class BackfillJob(models.Model):
    """Async backfill of labelling work over reports for a single ``QuestionSet``.

    The state machine is intentionally smaller than the prior version: the
    ``CANCELING`` state has been removed because cancellation is now fully
    synchronous (the cancel view writes ``CANCELED`` directly under a
    conditional UPDATE). See the cancel view for the conditional-update
    contract.
    """

    class Status(models.TextChoices):
        PENDING = "PE", "Pending"
        IN_PROGRESS = "IP", "In Progress"
        CANCELED = "CA", "Canceled"
        SUCCESS = "SU", "Success"
        FAILURE = "FA", "Failure"

    id: int
    question_set_id: int
    question_set = models.ForeignKey[QuestionSet](
        QuestionSet, on_delete=models.CASCADE, related_name="backfill_jobs"
    )
    status = models.CharField(max_length=2, choices=Status.choices, default=Status.PENDING)
    get_status_display: Callable[[], str]
    total_reports = models.PositiveIntegerField(default=0)
    processed_reports = models.PositiveIntegerField(default=0)
    message = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    ended_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"BackfillJob [{self.pk}]"

    @property
    def is_cancelable(self) -> bool:
        return self.status in [self.Status.PENDING, self.Status.IN_PROGRESS]

    @property
    def is_active(self) -> bool:
        return self.status in [self.Status.PENDING, self.Status.IN_PROGRESS]

    @property
    def is_retryable(self) -> bool:
        return self.status in [
            self.Status.FAILURE,
            self.Status.CANCELED,
            self.Status.SUCCESS,
        ]

    @property
    def is_terminal(self) -> bool:
        return self.status in [
            self.Status.SUCCESS,
            self.Status.FAILURE,
            self.Status.CANCELED,
        ]

    @property
    def processed_count(self) -> int:
        """Live progress for active jobs, frozen snapshot for terminal jobs.

        For terminal jobs we trust the snapshot stored at finalize/cancel
        time. For active jobs we derive the count live from
        ``QuestionSet.missing_reports``, so the progress display advances
        during the run without a hand-maintained counter that could race
        with cancellation.
        """
        if self.is_terminal:
            return self.processed_reports
        if self.total_reports == 0:
            return 0
        remaining = self.question_set.missing_reports().count()
        return max(self.total_reports - remaining, 0)

    @property
    def progress_percent(self) -> int:
        if self.total_reports == 0:
            return 0
        return min(int((self.processed_count / self.total_reports) * 100), 100)


def is_question_set_locked(question_set_id: int) -> bool:
    """Lightweight check used by forms/views without loading the full instance."""
    return BackfillJob.objects.filter(
        question_set_id=question_set_id,
        status__in=[BackfillJob.Status.PENDING, BackfillJob.Status.IN_PROGRESS],
    ).exists()


class EvalSample(models.Model):
    """A frozen, named sample of reports used for evaluation runs.

    The sample is captured up-front so the seed step (which enqueues
    labelling for missing runs) and the report step (which computes the
    comparison metrics) operate on exactly the same set of reports across
    invocations. Reports are pinned via M2M; deleting a report removes it
    from samples automatically.
    """

    id: int
    name = models.CharField(max_length=200, unique=True)
    description = models.TextField(blank=True, default="")
    question_set = models.ForeignKey[QuestionSet](
        QuestionSet, on_delete=models.CASCADE, related_name="eval_samples"
    )
    target_size = models.PositiveIntegerField()
    seed_value = models.IntegerField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    reports = models.ManyToManyField(Report, related_name="eval_samples")

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"EvalSample {self.name} [{self.pk}]"

    @property
    def actual_size(self) -> int:
        return self.reports.count()
