from django.db import migrations


CREATE_INDEX_SQL = """
CREATE UNIQUE INDEX labels_labelingjob_one_active_idx
ON labels_labelingjob ((1))
WHERE status IN ({active_values});
"""

DROP_INDEX_SQL = "DROP INDEX IF EXISTS labels_labelingjob_one_active_idx;"


class Migration(migrations.Migration):
    dependencies = [
        ("labels", "0003_labelingjob_labelingtask_and_more"),
    ]

    operations = [
        migrations.RunSQL(
            sql=CREATE_INDEX_SQL.format(
                active_values="'UV','PR','PE','IP','CI'"
            ),
            reverse_sql=DROP_INDEX_SQL,
        ),
    ]
