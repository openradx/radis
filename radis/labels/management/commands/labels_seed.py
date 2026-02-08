from __future__ import annotations

import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from ...models import LabelChoice, LabelGroup, LabelQuestion


class Command(BaseCommand):
    help = "Seed label groups, questions, and choices from a JSON file."

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
        groups = payload.get("groups", [])
        if not groups:
            self.stdout.write(self.style.WARNING("No groups found in seed file."))
            return

        for group_data in groups:
            group = self._upsert_group(group_data)
            for question_data in group_data.get("questions", []):
                question = self._upsert_question(group, question_data)
                for choice_data in question_data.get("choices", []):
                    self._upsert_choice(question, choice_data)

        self.stdout.write(self.style.SUCCESS("Label seed import completed."))

    def _upsert_group(self, data: dict) -> LabelGroup:
        slug = data.get("slug")
        name = data.get("name")
        if not slug or not name:
            raise CommandError("Label group requires 'slug' and 'name'.")

        group, _ = LabelGroup.objects.update_or_create(
            slug=slug,
            defaults={
                "name": name,
                "description": data.get("description", ""),
                "is_active": data.get("is_active", True),
                "order": data.get("order", 0),
            },
        )
        return group

    def _upsert_question(self, group: LabelGroup, data: dict) -> LabelQuestion:
        name = data.get("name")
        question_text = data.get("question")
        if not name or not question_text:
            raise CommandError("Label question requires 'name' and 'question'.")

        question, _ = LabelQuestion.objects.update_or_create(
            group=group,
            name=name,
            defaults={
                "question": question_text,
                "description": data.get("description", ""),
                "is_active": data.get("is_active", True),
                "order": data.get("order", 0),
            },
        )
        return question

    def _upsert_choice(self, question: LabelQuestion, data: dict) -> LabelChoice:
        value = data.get("value")
        label = data.get("label")
        if not value or not label:
            raise CommandError("Label choice requires 'value' and 'label'.")

        choice, _ = LabelChoice.objects.update_or_create(
            question=question,
            value=value,
            defaults={
                "label": label,
                "is_unknown": data.get("is_unknown", False),
                "order": data.get("order", 0),
            },
        )
        return choice
