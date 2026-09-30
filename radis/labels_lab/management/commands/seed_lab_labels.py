from typing import Any

from django.core.management.base import BaseCommand

from radis.labels.models import Label, LabelGroup

# Findings the synthetic example reports (samples/reports_*.json) mention often enough to
# try out, behind gate questions that ask for the examined body region.
EXAMPLE_GROUPS = {
    "Chest pathology": {
        "gate_question": "Does this report describe imaging of the chest?",
        "labels": {
            "pneumonia": "Infectious consolidation or infiltrate of the lung.",
            "pleural effusion": "Fluid in the pleural space.",
            "pneumothorax": "Air in the pleural space.",
            "rib fracture": "A fracture of one or more ribs.",
        },
    },
    "Acute abdomen": {
        "gate_question": "Does this report describe imaging of the abdomen or pelvis?",
        "labels": {
            "appendicitis": "Inflammation of the appendix.",
            "bowel obstruction": "Mechanical obstruction of the small or large bowel (ileus).",
            "free intraperitoneal air": (
                "Air in the abdominal cavity outside the bowel, as after a perforation."
            ),
        },
    },
    "Neuroimaging": {
        "gate_question": "Does this report describe imaging of the head or brain?",
        "labels": {
            "intracranial hemorrhage": "Bleeding inside the skull, of any type.",
            "ischemic stroke": "An acute or subacute infarction of brain tissue.",
            "intracranial mass": "A tumor or other space-occupying lesion inside the skull.",
        },
    },
}


class Command(BaseCommand):
    help = (
        "Create example label groups and labels for the label lab. Groups and labels that "
        "already exist by name are left as they are."
    )

    def handle(self, *args: Any, **options: Any) -> None:
        for group_name, example in EXAMPLE_GROUPS.items():
            group, group_created = LabelGroup.objects.get_or_create(
                name=group_name, defaults={"gate_question": example["gate_question"]}
            )
            self.stdout.write(f"{'Created' if group_created else 'Kept'} group '{group.name}'")

            for label_name, description in example["labels"].items():
                # Label names are unique across groups, so a label of that name may
                # already live in another group; it stays there.
                label, label_created = Label.objects.get_or_create(
                    name=label_name, defaults={"group": group, "description": description}
                )
                self.stdout.write(
                    f"  {'Created' if label_created else 'Kept'} label '{label.name}'"
                    f" in group '{label.group.name}'"
                )
