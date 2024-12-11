# Generated by Django 5.1 on 2024-11-13 10:51

from django.db import migrations, models

def populate_language_columns(apps, schema_editor):
    # Fetch models
    Language = apps.get_model("reports", "Language")
    Report = apps.get_model("reports", "Report")
    db_alias = schema_editor.connection.alias

    with schema_editor.connection.cursor() as cursor:
        # Populate each language column based on the `Language.code`
        for language in Language.objects.using(db_alias).all():
            column_name = f"body_{language.code}"
            cursor.execute(
                f"""
                UPDATE {Report._meta.db_table} AS r
                SET "{column_name}" = r.body
                FROM {Language._meta.db_table} AS l
                WHERE r.language_id = l.id AND l.code = %s
                """,
                [language.code],
            )

class Migration(migrations.Migration):

    dependencies = [
        ("reports", "0013_report_body_de_report_body_en"),
    ]

    operations = [
        migrations.RunPython(populate_language_columns),
    ]

