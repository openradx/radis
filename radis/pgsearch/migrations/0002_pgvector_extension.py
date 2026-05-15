from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [("pgsearch", "0001_initial")]
    operations = [
        migrations.RunSQL(
            sql="CREATE EXTENSION IF NOT EXISTS vector;",
            reverse_sql=migrations.RunSQL.noop,
        ),
    ]
