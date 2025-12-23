from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("extractions", "0004_outputfield_selection_options"),
    ]

    operations = [
        migrations.AddField(
            model_name="outputfield",
            name="is_array",
            field=models.BooleanField(default=False),
        ),
    ]
