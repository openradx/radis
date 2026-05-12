"""Create an EvalSample, pick reports stratified by year, and enqueue any
missing labelling runs.

Usage::

    uv run cli shell  # then in shell:
    or
    uv run cli manage labels_eval_seed --question-set-id 1 --sample 5000

The command prints a rough work/cost estimate before enqueueing. It does
NOT block on completion; the labelling pipeline runs asynchronously on the
``llm`` worker.
"""

from __future__ import annotations

from datetime import UTC, datetime

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from radis.reports.models import Report

from ...models import EvalSample, LabelingRun, QuestionSet
from ...tasks import enqueue_labeling_for_reports
from ...utils.eval_sampler import estimate_calls, sample_reports


class Command(BaseCommand):
    help = "Create an EvalSample for a QuestionSet and enqueue missing labelling."

    def add_arguments(self, parser):
        parser.add_argument(
            "--question-set-id",
            dest="question_set_id",
            type=int,
            required=True,
        )
        parser.add_argument(
            "--sample",
            dest="sample_size",
            type=int,
            default=5000,
            help="Target number of reports in the sample.",
        )
        parser.add_argument(
            "--name",
            dest="name",
            default=None,
            help="Unique name for the EvalSample. Auto-generated from timestamp if omitted.",
        )
        parser.add_argument(
            "--seed",
            dest="seed",
            type=int,
            default=42,
        )
        parser.add_argument(
            "--description",
            dest="description",
            default="",
        )

    def handle(self, *args, **options):
        try:
            question_set = QuestionSet.objects.get(pk=options["question_set_id"])
        except QuestionSet.DoesNotExist as exc:
            raise CommandError(
                f"QuestionSet {options['question_set_id']} not found."
            ) from exc

        active_questions = question_set.questions.filter(is_active=True).count()
        if active_questions == 0:
            raise CommandError(
                f"QuestionSet '{question_set.name}' has no active questions."
            )

        target_size = options["sample_size"]
        seed = options["seed"]
        name = options["name"] or self._default_name(question_set.name)
        description = options["description"]

        if EvalSample.objects.filter(name=name).exists():
            raise CommandError(
                f"EvalSample named '{name}' already exists. Choose another name."
            )

        report_ids = sample_reports(target_size=target_size, seed=seed)
        if not report_ids:
            self.stdout.write(self.style.WARNING("No reports available to sample."))
            return

        sample = EvalSample.objects.create(
            name=name,
            description=description,
            question_set=question_set,
            target_size=target_size,
            seed_value=seed,
        )
        sample.reports.set(Report.objects.filter(id__in=report_ids))

        modes = getattr(settings, "LABELS_RUN_MODES", [LabelingRun.Mode.DIRECT])
        estimate = estimate_calls(
            sample_size=sample.actual_size,
            active_questions=active_questions,
            modes=modes,
        )

        self.stdout.write(self.style.SUCCESS(f"Created EvalSample '{name}' (#{sample.id})"))
        self.stdout.write(f"  Reports pinned: {sample.actual_size} / target {target_size}")
        self.stdout.write(f"  Active questions: {active_questions}")
        self.stdout.write(f"  Modes: {modes}")
        self.stdout.write(
            f"  Estimated LLM calls: {estimate['total_calls']:,} "
            f"(~{estimate['estimated_tokens']:,} tokens at "
            f"{estimate['estimated_tokens'] // estimate['total_calls']} tokens/call)"
        )

        # missing_reports semantics: a report needs SUCCESS runs for every
        # mode to be considered complete. Reusing it here means the seed
        # command is a no-op if the nightly backfill has already covered
        # the sample, which is what we want.
        missing = question_set.missing_reports().filter(id__in=report_ids)
        missing_ids = list(missing.values_list("id", flat=True))
        if not missing_ids:
            self.stdout.write(
                self.style.SUCCESS(
                    "All sampled reports already have complete runs. Nothing to enqueue."
                )
            )
            return

        enqueue_labeling_for_reports(missing_ids, question_sets=[question_set])
        self.stdout.write(
            self.style.SUCCESS(
                f"Enqueued labelling for {len(missing_ids)} sampled reports "
                f"that did not yet have complete runs."
            )
        )

    @staticmethod
    def _default_name(question_set_name: str) -> str:
        stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
        slug = question_set_name.lower().replace(" ", "-")
        return f"eval-{slug}-{stamp}"
