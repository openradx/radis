# Generated by Django 4.2.7 on 2023-12-11 21:36

from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0007_convert_institutes_to_groups"),
    ]

    operations = [
        migrations.RemoveField(
            model_name="institute",
            name="users",
        ),
    ]
