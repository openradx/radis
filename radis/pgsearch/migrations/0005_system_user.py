from django.conf import settings
from django.db import migrations

from radis.pgsearch.migrations._system_user_helper import create_system_user_idempotent


def forwards(apps, schema_editor):
    User = apps.get_model(*settings.AUTH_USER_MODEL.split("."))
    create_system_user_idempotent(User)


class Migration(migrations.Migration):
    dependencies = [
        ("pgsearch", "0004_embedding_job_task"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]
    operations = [migrations.RunPython(forwards, reverse_code=migrations.RunPython.noop)]
