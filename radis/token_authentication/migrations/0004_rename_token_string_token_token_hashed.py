# Generated by Django 4.2.3 on 2023-07-23 13:53

from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("token_authentication", "0003_alter_token_client"),
    ]

    operations = [
        migrations.RenameField(
            model_name="token",
            old_name="token_string",
            new_name="token_hashed",
        ),
    ]
