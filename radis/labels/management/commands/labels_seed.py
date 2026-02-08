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
                if question_data.get("choices"):
                    self._upsert_choice(question, {})

        self.stdout.write(self.style.SUCCESS("Label seed import completed."))

    def _upsert_group(self, data: dict) -> LabelGroup:
        name = data.get("name")
        if not name:
            raise CommandError("Label group requires 'name'.")

        groups = LabelGroup.objects.filter(name=name)
        if groups.count() > 1:
            raise CommandError(
                f"Multiple label groups named '{name}' exist. Use unique names before seeding."
            )

        defaults = {
            "description": data.get("description", ""),
            "is_active": data.get("is_active", True),
            "order": data.get("order", 0),
        }

        if groups.exists():
            group = groups.first()
            if group is None:
                raise CommandError(
                    f"Label group '{name}' could not be resolved. Try seeding again."
                )
            for key, value in defaults.items():
                setattr(group, key, value)
            group.save(update_fields=list(defaults.keys()))
            return group

        return LabelGroup.objects.create(name=name, **defaults)

    def _upsert_question(self, group: LabelGroup, data: dict) -> LabelQuestion:
        label = data.get("label")
        question_text = data.get("question", "")
        if not label:
            raise CommandError("Label question requires 'label'.")

        question, _ = LabelQuestion.objects.update_or_create(
            group=group,
            label=label,
            defaults={
                "question": question_text,
                "is_active": data.get("is_active", True),
                "order": data.get("order", 0),
            },
        )
        return question

    def _upsert_choice(self, question: LabelQuestion, data: dict) -> LabelChoice:
        raise CommandError(
            "Custom choices are not supported. Labels use fixed Yes/No/Cannot decide choices."
        )
