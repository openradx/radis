from __future__ import annotations

import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from pydantic import ValidationError

from ...models import Question, QuestionSet
from ...schemas import QuestionSetSchema


class Command(BaseCommand):
    help = "Seed question sets and questions from a JSON file. Answer options are auto-created."

    def add_arguments(self, parser):
        parser.add_argument(
            "--file",
            dest="file",
            default="resources/labels/seed.json",
            help="Path to the seed JSON file.",
        )

    def handle(self, *args, **options):
        seed_path = Path(options["file"]).resolve()
        if not seed_path.exists():
            raise CommandError(f"Seed file not found: {seed_path}")

        payload = json.loads(seed_path.read_text())
        question_sets = payload.get("question_sets") or payload.get("groups") or []
        if not question_sets:
            self.stdout.write(self.style.WARNING("No question_sets found in seed file."))
            return

        for set_data in question_sets:
            try:
                schema = QuestionSetSchema.model_validate(set_data)
            except ValidationError as exc:
                raise CommandError(f"Invalid question set in seed: {exc}") from exc
            self._upsert_set(schema)

        self.stdout.write(self.style.SUCCESS("Label seed import completed."))

    def _upsert_set(self, schema: QuestionSetSchema) -> QuestionSet:
        question_sets = QuestionSet.objects.filter(name=schema.name)
        if question_sets.count() > 1:
            raise CommandError(
                f"Multiple question sets named '{schema.name}' exist. "
                "Use unique names before seeding."
            )

        defaults = {
            "description": schema.description,
            "is_active": schema.is_active,
            "order": schema.order,
        }

        if question_sets.exists():
            question_set = question_sets.first()
            assert question_set is not None
            for key, value in defaults.items():
                setattr(question_set, key, value)
            question_set.save(update_fields=list(defaults.keys()))
        else:
            question_set = QuestionSet.objects.create(name=schema.name, **defaults)

        for question_schema in schema.questions:
            Question.objects.update_or_create(
                question_set=question_set,
                label=question_schema.label,
                defaults={
                    "question": question_schema.question,
                    "is_active": question_schema.is_active,
                    "order": question_schema.order,
                },
            )
        return question_set
