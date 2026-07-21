from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("extractions", "0004_remove_extractionjob_provider"),
    ]

    operations = [
        migrations.AddField(
            model_name="outputfield",
            name="selection_options",
            field=models.JSONField(blank=True, default=list),
        ),
        migrations.AlterField(
            model_name="outputfield",
            name="output_type",
            field=models.CharField(
                choices=[
                    ("T", "Text"),
                    ("N", "Numeric"),
                    ("B", "Boolean"),
                    ("S", "Selection"),
                ],
                default="T",
                max_length=1,
            ),
        ),
        migrations.AddField(
            model_name="outputfield",
            name="is_array",
            field=models.BooleanField(default=False),
        ),
        migrations.RemoveField(
            model_name="outputfield",
            name="optional",
        ),
    ]
