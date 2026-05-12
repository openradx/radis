"""Compute DIRECT vs REASONED comparison metrics for an EvalSample.

Writes a Markdown report to ``evals/<question_set>_<timestamp>.md`` (under
the project root) and echoes a summary to stdout.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from ...models import EvalSample
from ...utils.eval_metrics import compute_eval, render_markdown


class Command(BaseCommand):
    help = "Compute DIRECT vs REASONED metrics for an EvalSample and write a Markdown report."

    def add_arguments(self, parser):
        parser.add_argument("--sample-id", dest="sample_id", type=int, default=None)
        parser.add_argument("--sample-name", dest="sample_name", default=None)
        parser.add_argument(
            "--top",
            dest="top",
            type=int,
            default=20,
            help="Maximum number of disagreement examples to include.",
        )
        parser.add_argument(
            "--output-dir",
            dest="output_dir",
            default="evals",
            help="Directory the Markdown report is written to (relative to CWD).",
        )

    def handle(self, *args, **options):
        sample = self._resolve_sample(options)

        report = compute_eval(sample, top_disagreements=options["top"])
        markdown = render_markdown(report)

        output_dir = Path(options["output_dir"]).resolve()
        output_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
        safe_set = sample.question_set.name.lower().replace(" ", "-")
        out_path = output_dir / f"{safe_set}_{stamp}.md"
        out_path.write_text(markdown)

        overall = report["overall"]
        if overall["n_compared"]:
            self.stdout.write(
                f"Overall agreement: {overall['n_agree']} / {overall['n_compared']} "
                f"({overall['agreement_rate']:.1%})"
            )
        else:
            self.stdout.write(
                "No comparable (DIRECT + REASONED) answers exist yet for this sample."
            )

        self.stdout.write(self.style.SUCCESS(f"Wrote report to {out_path}"))

    def _resolve_sample(self, options) -> EvalSample:
        if options["sample_id"]:
            try:
                return EvalSample.objects.get(pk=options["sample_id"])
            except EvalSample.DoesNotExist as exc:
                raise CommandError(f"EvalSample {options['sample_id']} not found.") from exc
        if options["sample_name"]:
            try:
                return EvalSample.objects.get(name=options["sample_name"])
            except EvalSample.DoesNotExist as exc:
                raise CommandError(
                    f"EvalSample named '{options['sample_name']}' not found."
                ) from exc
        raise CommandError("Pass either --sample-id or --sample-name.")
