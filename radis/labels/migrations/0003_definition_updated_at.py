import django.utils.timezone
from django.db import migrations, models
from django.db.models import F

DEFINITION_FIELDS = {"LabelGroup": ("name", "gate_question"), "Label": ("name", "description")}


def _normalize(value: str) -> str:
    return value.replace("\r\n", "\n").replace("\r", "\n").strip()


def copy_updated_at_and_normalize(apps, schema_editor):
    """Existing results stay exactly as fresh or stale as they were: the new field starts at
    the value staleness was measured against so far. Stored definition text is normalized so
    the admin never shows a spurious line-ending or whitespace change (change detection
    normalizes both sides anyway). Not undone on rollback."""
    for model_name, fields in DEFINITION_FIELDS.items():
        model = apps.get_model("labels", model_name)
        # Names are unique, so two that differ only in surrounding whitespace would collide.
        by_normalized_name: dict[str, list[str]] = {}
        for name in model.objects.values_list("name", flat=True):
            by_normalized_name.setdefault(_normalize(name), []).append(repr(name))
        collisions = [names for names in by_normalized_name.values() if len(names) > 1]
        if collisions:
            raise RuntimeError(
                f"labels.{model_name}: these names would collide once surrounding whitespace is "
                f"stripped: {collisions}. Rename them, then migrate again."
            )
        model.objects.update(definition_updated_at=F("updated_at"))
        for obj in model.objects.all():
            normalized = {f: _normalize(getattr(obj, f)) for f in fields}
            if any(normalized[f] != getattr(obj, f) for f in fields):
                model.objects.filter(pk=obj.pk).update(**normalized)


class Migration(migrations.Migration):
    dependencies = [
        ("labels", "0002_procrastinate_on_delete"),
    ]

    operations = [
        migrations.AddField(
            model_name="labelgroup",
            name="definition_updated_at",
            field=models.DateTimeField(null=True, editable=False),
        ),
        migrations.AddField(
            model_name="label",
            name="definition_updated_at",
            field=models.DateTimeField(null=True, editable=False),
        ),
        migrations.RunPython(copy_updated_at_and_normalize, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="labelgroup",
            name="definition_updated_at",
            field=models.DateTimeField(default=django.utils.timezone.now, editable=False),
        ),
        migrations.AlterField(
            model_name="label",
            name="definition_updated_at",
            field=models.DateTimeField(default=django.utils.timezone.now, editable=False),
        ),
    ]
