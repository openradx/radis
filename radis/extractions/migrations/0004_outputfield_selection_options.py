from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("extractions", "0003_alter_extractionjob_options_and_more"),
    ]

    operations = [

        # Introduce a JSON column to store the Selection output fields.
        migrations.AddField(
            model_name="outputfield",
            name="selection_options",
            field=models.JSONField(blank=True, default=list),
        ),

        # Extend the output_type choices so the database accepts the new Selection code ("S").
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
    ]
