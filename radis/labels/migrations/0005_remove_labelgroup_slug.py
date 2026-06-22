from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("labels", "0004_add_label_to_labelquestion"),
    ]

    operations = [
        migrations.RemoveField(
            model_name="labelgroup",
            name="slug",
        ),
    ]
