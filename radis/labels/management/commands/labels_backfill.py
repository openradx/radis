"""Launch backfill jobs for question sets with outstanding labelling work.

This command is the CLI equivalent of the "Run backfill now" button in the
UI. It shares the dispatch path with the nightly launcher, the manual
launch view, and the Retry button via :func:`dispatch_backfill_for_set`,
which means all four callers obey the same per-set lock and dedup
contract — running this command while a backfill is in flight is safe and
will simply log a "already active" notice rather than stacking a parallel
job.

History (HIGH #1 of the 2026-05-19 review): the previous version of this
command enqueued every report in the corpus regardless of existing
coverage. Running it on a fully-labelled set would re-label every report,
producing duplicate ``LabelingRun`` and ``Answer`` rows and burning the
LLM bill for nothing. It also did not take the dispatch lock, so it could
stack on top of an in-flight nightly. The rewrite routes through the
coordinator (which dispatches ``missing_reports()`` only) so re-running
on a fully-labelled set is now a no-op.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from ...models import QuestionSet
from ...tasks import dispatch_backfill_for_set


class Command(BaseCommand):
    help = (
        "Launch backfill jobs for question sets with outstanding labelling "
        "work. Safe to run while other backfills are in flight (each set is "
        "deduplicated under a row lock)."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--set",
            dest="question_set",
            help=(
                "QuestionSet name or numeric ID. If omitted, every active set "
                "with outstanding work gets a backfill dispatched."
            ),
        )

    def handle(self, *args, **options):
        value = options.get("question_set")

        if value:
            question_sets = [self._get_set(value)]
        else:
            question_sets = list(QuestionSet.objects.filter(is_active=True))

        if not question_sets:
            self.stdout.write(self.style.WARNING("No active question sets found."))
            return

        for question_set in question_sets:
            # We mirror BackfillLaunchView's guards here. The helper itself
            # only enforces the lock + dedup; "the set is inactive" and "the
            # set has no missing reports" are caller-level decisions about
            # what counts as a useful invocation, surfaced as CLI messages.
            if not question_set.is_active:
                self.stdout.write(
                    self.style.NOTICE(
                        f"Skipping '{question_set.name}' — set is inactive."
                    )
                )
                continue

            if not question_set.missing_reports().exists():
                self.stdout.write(
                    self.style.NOTICE(
                        f"Skipping '{question_set.name}' — no outstanding labelling work."
                    )
                )
                continue

            backfill_job = dispatch_backfill_for_set(question_set)
            if backfill_job is None:
                # A backfill for this set is already pending or in progress.
                # The CLI defers to the existing job rather than queueing a
                # duplicate; this preserves HIGH #1's "no duplicate runs"
                # property when the CLI is invoked alongside the nightly
                # launcher or a clicked Launch button.
                self.stdout.write(
                    self.style.WARNING(
                        f"A backfill for '{question_set.name}' is already "
                        "active; skipping."
                    )
                )
                continue

            self.stdout.write(
                self.style.SUCCESS(
                    f"Started backfill job #{backfill_job.id} for "
                    f"'{question_set.name}'."
                )
            )

    def _get_set(self, value: str) -> QuestionSet:
        if value.isdigit():
            question_set = QuestionSet.objects.filter(id=int(value)).first()
        else:
            matches = QuestionSet.objects.filter(name=value)
            if matches.count() > 1:
                raise CommandError(
                    f"Multiple question sets named '{value}' exist. Use the numeric ID."
                )
            question_set = matches.first()

        if not question_set:
            raise CommandError(f"Question set '{value}' not found.")

        return question_set
