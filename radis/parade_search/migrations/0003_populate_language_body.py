# Generated by Django 5.1.3 on 2024-12-11 16:00

from django.db import migrations, models

def populate_language_columns(apps, schema_editor):
    # Fetch models
    Language = apps.get_model("reports", "Language")
    ParadeDBReport = apps.get_model("parade_search", "ParadeDBReport")
    Report = apps.get_model("reports", "Report")
    db_alias = schema_editor.connection.alias

    with schema_editor.connection.cursor() as cursor:
        # Populate each language column based on the `Language.code`
        for language in Language.objects.using(db_alias).all():
            column_name = f"body_{language.code}"
            print(f"Populating {column_name} column")
            cursor.execute(
                f"""
                UPDATE {ParadeDBReport._meta.db_table} AS pdr
                SET "{column_name}" = r.body
                FROM {Report._meta.db_table} AS r
                INNER JOIN {Language._meta.db_table} AS l
                ON r.language_id = l.id
                WHERE pdr.report_id = r.id AND l.code = %s
                """,
                [language.code],
            )


class Migration(migrations.Migration):

    dependencies = [
        ("parade_search", "0002_paradedbreport_delete_reportsearchvectornew"),
    ]

    operations = [
        migrations.RunPython(populate_language_columns),
    ]